"""Frozen company→raw-document offline experiment, using shared retriever engines."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import subprocess
import time
import numpy as np
from ranking.profiles import CompanyRepository, industry_for
from retriever.raw_queries import build_queries
from retriever.raw_search import RawLexicalIndex, exact_topk, family_fusion
from llm.event_pipeline.semantic.core import publication, interval
from llm.event_pipeline.semantic.experiment import make_units
from .contracts import RawNewsDocument, NewsDocumentRef, CompanyQuery, ExperimentManifest, fingerprint, text_hash, instant
from .io import read, rows, save, save_rows, sha, code_hashes, load_plan, complete, require

AS_OF = '2026-10-23T00:00:00+00:00'
VARIANTS = dict(C0=('lexical', ['facts']), C1=('e5', ['facts']), C2=('e5+lexical', ['facts']),
    G0=('bge+lexical', ['facts']), D0=('bge', ['facts']), G1=('bge+lexical', ['facts','mechanisms']),
    G2=('bge+lexical', ['facts','mechanisms','hypotheses']),
    A_fusion=('bge+lexical', ['facts','mechanisms','hypotheses']),
    A_freshness=('bge+lexical', ['facts','mechanisms','hypotheses']))


def eligible(doc, as_of):
    r = doc['reference']
    window = interval(dict(precision=r['publication_precision'], value=r['published_at']))
    if window is None:
        return 'publication_date_unknown'
    if window[1] > instant(as_of).timestamp():
        return 'publication_not_fully_available_at_as_of'
    if r.get('observed_at') and instant(r['observed_at']) > instant(as_of):
        return 'observed_after_as_of'
    return None


def embedding_text(doc):
    return (doc['title']+'\n\n' if doc['title_origin']=='source' and doc['title'] else '')+doc['text']


def plan(root, corpus, companies, catalogue, *, as_of=AS_OF):
    root, corpus = Path(root), Path(corpus)
    if (root/'manifest.json').exists():
        raise FileExistsError('plan_exists_choose_new_run')
    root.mkdir(parents=True, exist_ok=True)
    source = read(corpus/'manifest.json')
    if source['schema_version']!='prepared-news.v1' or sha(corpus/'documents.jsonl') != source['outputs']['documents.jsonl']:
        raise ValueError('prepared_snapshot_hash_mismatch')
    docs, exclusions = [], []
    from llm.events.contracts.models import ArticleInput
    from llm.events.contracts.validation import article_sha256
    for d in sorted(rows(corpus/'documents.jsonl'), key=lambda d:d['document_id']):
        a=d['article']; body=a['text']
        if article_sha256(ArticleInput.model_validate(a)) != d['article_sha256'] or text_hash(body) != d['text_sha256']:
            raise ValueError('source_hash_mismatch')
        reason='empty_body' if not body.strip() else 'original_body_over_2000_chars' if len(body)>2000 else None
        if reason:
            exclusions.append(dict(document_id=d['document_id'], reason=reason, body_chars=len(body), article_version=d['article_sha256']))
            continue
        date=publication(d)
        technical=any(p.get('title_origin')=='generated_identifier' for p in d['provenance'])
        ref=NewsDocumentRef(document_id=d['document_id'], article_id=a['article_id'], article_version=d['article_sha256'],
            content_hash=text_hash(body), snapshot_id=source['corpus_id'], source=a['source'],
            published_at=date['value'], publication_precision=date['precision'])
        doc=RawNewsDocument(reference=ref,title=a['title'],title_origin='generated_identifier' if technical else 'source',text=body,provenance=d['provenance']).model_dump()
        # Confirm representation compatibility with the previous semantic encoder.
        if embedding_text(doc) != make_units(d,2000)[0]['embedding_text']:
            raise ValueError('embedding_text_policy_mismatch')
        docs.append(doc)
    repo=CompanyRepository(companies, imported_at=as_of)
    # Diversify without looking at retrieval results or labels.
    chosen=[];seen=set()
    available=sorted(repo.company_ids-repo.conflicts)
    for uid in available:
        raw=repo.rows[uid][0]
        key=industry_for(raw['MAINOKVED_CODE'],repo.config)
        if key not in seen:
            chosen.append(uid);seen.add(key)
    # Include foreign trade, loan, deposit, guarantee and activities absent above.
    for field in ('FTS_FLAG','CL_PRODUCT_LOA_MARK','CL_PRODUCT_DEP_MARK','CL_PRODUCT_GAR_MARK'):
        for uid in available:
            if uid not in chosen and repo.rows[uid][0].get(field) in ('1','1.0'):
                chosen.append(uid);break
    used={repo.rows[u][0]['MAINOKVED_CODE'] for u in chosen}
    for uid in available:
        if len(chosen)>=20:break
        code=repo.rows[uid][0]['MAINOKVED_CODE']
        if uid not in chosen and code not in used:
            chosen.append(uid);used.add(code)
    profiles=[repo.get(uid) for uid in chosen]
    catrows=read(catalogue); cat={r['okved_code']:r for r in catrows}
    queries=[];skip=[]
    for p in profiles:
        qs, skips=build_queries(p,cat);queries.extend(q.model_dump() for q in qs)
        skip.extend(dict(company_id=p['company_id'],**s) for s in skips)
    perf_ids=random.Random(20261023).sample([u for u in available if u not in chosen],128)
    perf=[]
    for uid in perf_ids:
        qs,_=build_queries(repo.get(uid),cat);perf.extend(q.model_dump() for q in qs)
    # Freeze story split before judgments/selection. Unit groups are reading groups, not identities.
    comparison=Path('data/processed/event_pipeline/gpu-experiment/comparisons/full-chosen')
    units={u['unit_id']:u['document_id'] for u in rows(comparison/'units.jsonl')}
    groups=[]
    for g in rows(comparison/'semantic_time.groups.jsonl'):
        ids=sorted({units[u] for u in g['unit_ids']})
        groups.append(dict(group_id=g['group_id'],document_ids=ids,interpretation=g['interpretation']))
    # Connected story components across semantic AND temporal groups prevent duplicate leakage.
    parent={d['reference']['document_id']:d['reference']['document_id'] for d in docs}
    def find(x):
        while x!=parent[x]:parent[x]=parent[parent[x]];x=parent[x]
        return x
    for variant in ('semantic','semantic_time'):
        for g in rows(comparison/(variant+'.groups.jsonl')):
            ids=sorted({units[u] for u in g['unit_ids']} & parent.keys())
            for uid in ids[1:]:
                a,b=find(ids[0]),find(uid);parent[max(a,b)]=min(a,b)
    stories={uid:find(uid) for uid in parent}
    split={uid:('dev' if int(fingerprint(story)[:8],16)%3==0 else 'test') for uid,story in stories.items()}
    limits=dict(per_query_topk=50, union_cap=250, pool_cap=100, final_documents=20, context_utf8_bytes=24000,
        token_counter='utf8_bytes_div4_estimate_only', max_hypotheses_per_company=2, max_calls=None,live_budget=None)
    manifest=ExperimentManifest(experiment_id=fingerprint(dict(snapshot=source['corpus_id'],companies=repo.sha256,queries=queries,as_of=as_of)),
        as_of=as_of,snapshot_id=source['corpus_id'],inputs=dict(corpus=sha(corpus/'documents.jsonl'),companies=repo.sha256,catalogue=sha(catalogue),
            semantic_groups=sha(comparison/'semantic.groups.jsonl'),temporal_groups=sha(comparison/'semantic_time.groups.jsonl')),
        profile_versions={p['company_id']:p['profile_version'] for p in profiles}, code=code_hashes(),
        models=dict(bge=dict(model='BAAI/bge-m3',revision='5617a9f61b028005a4858fdac845db406aefb181'),
            e5=dict(model='intfloat/multilingual-e5-small',revision='614241f622f53c4eeff9890bdc4f31cfecc418b3')),
        representations=dict(document='source-title-plus-original-body.v1',company='base3+anchored-mechanisms+activity-hypotheses.v1'),
        fusion=dict(version='family-channel-max-rrf.v1',k=60,family_weights=dict(facts=1.,mechanisms=.5,hypotheses=.35,graph=.2),
            channel_weights=dict(dense=1.,lexical=1.),control='max-across-all-queries-per-channel:existing-retriever-equivalent'),
        budget=limits,split=dict(policy='connected-semantic-and-temporal-groups-sha-mod3.v1',gold='pending_manual_labels',control344='not_available_local_old_control_has_100'),
        state='frozen_offline_plan',resources=dict(cpu_gpu_separate=True,profile_temporal_status='static_snapshot_historical_availability_unproven',graph='not_run_no_historical_availability'))
    save(root/'manifest.json',manifest.model_dump());(root/'manifest.sha256').write_text(fingerprint(manifest.model_dump()))
    files={'documents.jsonl':docs,'excluded.jsonl':exclusions,'profiles.jsonl':profiles,'queries.jsonl':queries,'query-skips.jsonl':skip,
           'performance-queries.jsonl':perf,'groups.jsonl':groups,'story-split.jsonl':[dict(document_id=u,story_id=stories[u],split=split[u]) for u in sorted(stories)]}
    for name,values in files.items():save_rows(root/name,values)
    save(root/'plan-files.json',{name:sha(root/name) for name in files})
    counts=dict(total=len(docs)+len(exclusions),short=len(docs),excluded=len(exclusions),eligible=sum(eligible(d,as_of) is None for d in docs),
        date_exclusions=dict(Counter(eligible(d,as_of) or 'eligible' for d in docs)),companies=len(profiles),queries=len(queries),unique_query_texts=len({q['text'] for q in queries}),
        sources=dict(Counter(d['reference']['source'] for d in docs)),profile_conflicts=len(repo.conflicts),performance_companies=len(perf_ids))
    save(root/'inventory.json',counts)
    return counts


def index(root, *, batch_size=8):
    from retriever.raw_encoder import BGEEncoder
    from retriever.embeddings import Encoder
    from retriever.representation_contracts import EncoderConfig
    root=Path(root);m=load_plan(root); docs=rows(root/'documents.jsonl');queries=rows(root/'queries.jsonl')+rows(root/'performance-queries.jsonl')
    texts=[embedding_text(d) for d in docs]; unique=list(dict.fromkeys(q['text'] for q in queries))
    outputs=[];stats={}
    for name in ('bge','e5'):
        start=time.perf_counter()
        encoder=BGEEncoder(root/'bge-cache.sqlite3',batch_size=batch_size,old_run='data/processed/event_pipeline/gpu-experiment/full-run') if name=='bge' else Encoder(EncoderConfig(model_dir='ranking/experiments/cpu_news_embeddings/artifacts/model',batch_size=8,threads=2))
        load_seconds=time.perf_counter()-start
        lengths=encoder.token_lengths(texts,input_kind='document'); qlengths=encoder.token_lengths(unique,input_kind='query')
        cap=8192 if name=='bge' else 512
        di=[i for i,n in enumerate(lengths) if n<=cap];qi=[i for i,n in enumerate(qlengths) if n<=cap]
        start=time.perf_counter();vectors=encoder.encode_documents([texts[i] for i in di]);doc_seconds=time.perf_counter()-start
        start=time.perf_counter();qvectors=encoder.encode_queries([unique[i] for i in qi]);query_seconds=time.perf_counter()-start
        np.save(root/(name+'-documents.npy'),vectors);np.save(root/(name+'-queries.npy'),qvectors)
        meta=dict(fingerprint=encoder.fingerprint,profile=encoder.fingerprint_metadata,document_ids=[docs[i]['reference']['document_id'] for i in di],
            query_texts=[unique[i] for i in qi],document_token_lengths=lengths,query_token_lengths=qlengths,
            exclusions=[dict(document_id=docs[i]['reference']['document_id'],stage=name,reason='encoder_token_limit',tokens=n) for i,n in enumerate(lengths) if n>cap])
        save(root/(name+'-index.json'),meta)
        stats[name]=dict(load_seconds=load_seconds,documents_seconds=doc_seconds,queries_seconds=query_seconds,documents=len(di),queries=len(qi),
            excluded_documents=len(docs)-len(di),excluded_queries=len(unique)-len(qi),details=getattr(encoder,'metrics',{}))
        outputs.extend(root/(name+s) for s in ('-documents.npy','-queries.npy','-index.json'))
        if name=='bge':
            encoder.cache.close()
            if encoder.old:encoder.old.close()
            del encoder
            import gc,torch
            gc.collect();torch.cuda.empty_cache()
    complete(root,'index',outputs,stats)
    return stats


def retrieve(root):
    root=Path(root);m=load_plan(root);require(root,'index');docs=rows(root/'documents.jsonl');qs=rows(root/'queries.jsonl')
    eligible_ids={d['reference']['document_id'] for d in docs if eligible(d,m['as_of']) is None}
    lexical=RawLexicalIndex({d['reference']['document_id']:embedding_text(d) for d in docs if d['reference']['document_id'] in eligible_ids})
    start=time.perf_counter();lex={text:lexical.search(text) for text in dict.fromkeys(q['text'] for q in qs)};lex_seconds=time.perf_counter()-start
    dense={};perfstats={};metas={}
    for name in ('bge','e5'):
        meta=read(root/(name+'-index.json'));metas[name]=meta
        matrix=np.load(root/(name+'-documents.npy'),mmap_mode='r');qmatrix=np.load(root/(name+'-queries.npy'),mmap_mode='r')
        ids=meta['document_ids'];positions=[i for i,uid in enumerate(ids) if uid in eligible_ids];qmap={s:i for i,s in enumerate(meta['query_texts'])}
        def blocks():
            for off in range(0,len(positions),512):
                ix=positions[off:off+512];yield [ids[i] for i in ix],matrix[ix]
        dense[name]={};elapsed=[]
        texts=list(dict.fromkeys(q['text'] for q in qs if q['text'] in qmap))
        for off in range(0,len(texts),16):
            batch=texts[off:off+16];start=time.perf_counter();result=exact_topk(qmatrix[[qmap[t] for t in batch]],blocks());elapsed.append(time.perf_counter()-start)
            dense[name].update(zip(batch,result))
        # Separate larger performance sample; no quality claims for these companies.
        perfqs=rows(root/'performance-queries.jsonl');bycompany=defaultdict(list)
        for q in perfqs:
            if q['text'] in qmap:bycompany[q['company_id']].append(qmap[q['text']])
        times=[]
        for indices in bycompany.values():
            start=time.perf_counter();exact_topk(qmatrix[indices],blocks());times.append(time.perf_counter()-start)
        perfstats[name]=dict(quality_dense_seconds=sum(elapsed),performance_companies=len(times),p50_seconds=float(np.median(times)),p95_seconds=float(np.percentile(times,95)),
            companies_per_second=len(times)/sum(times),maximum_score_matrix_shape=[16,512],cosine_backend='numpy-cpu-exact-on-GPU-encoded-vectors')
    bycompany=defaultdict(list)
    for q in qs:bycompany[q['company_id']].append(q)
    results=[]
    for uid,queries in bycompany.items():
        for variant,(channels,families) in VARIANTS.items():
            hits=[]
            for q in queries:
                if q['family'] not in families:continue
                if 'lexical' in channels:
                    hits.extend(dict(document_id=h['document_id'],query_id=q['query_id'],family=q['family'],channel='lexical',rank=h['rank'],raw_score=h['raw_score']) for h in lex[q['text']])
                enc='bge' if 'bge' in channels else 'e5' if 'e5' in channels else None
                if enc:
                    hits.extend(dict(document_id=did,query_id=q['query_id'],family=q['family'],channel='dense',rank=rank,raw_score=score,encoder_fingerprint=metas[enc]['fingerprint']) for rank,(did,score) in enumerate(dense[enc].get(q['text'],[]),1))
            if variant=='A_fusion':
                # Existing retriever control: max across views per channel, preserving real family in provenance.
                collapsed=[dict(h,family='facts') for h in hits];ordered=family_fusion(collapsed)
                original=defaultdict(list)
                for h in hits:original[h['document_id']].append(h)
                for r in ordered:r['hits']=original[r['document_id']]
            else:ordered=family_fusion(hits)
            if variant=='A_freshness':
                dmap={d['reference']['document_id']:d for d in docs}
                for r in ordered:
                    d=dmap[r['document_id']]['reference'];end=interval(dict(precision=d['publication_precision'],value=d['published_at']))[1]
                    r['freshness_factor']=.5+.5*2**(-(instant(m['as_of']).timestamp()-end)/86400/30)
                    r['fusion_score']*=r['freshness_factor']
                ordered.sort(key=lambda r:(-r['fusion_score'],r['document_id']))
                for rank,r in enumerate(ordered,1):r.update(fusion_rank=rank,exclusion_stage='retrieval' if rank>100 else None,exclusion_reason='union_cap' if rank>250 else 'pool_cap' if rank>100 else None)
            results.append(dict(company_id=uid,variant=variant,candidates=ordered))
    save_rows(root/'retrieval.jsonl',results)
    complete(root,'retrieve',[root/'retrieval.jsonl'],dict(lexical_seconds=lex_seconds,performance=perfstats,variants=list(VARIANTS)))
    return dict(rows=len(results),metrics=perfstats)
