from collections import defaultdict
from pathlib import Path
import time
from retriever.raw_reranker import CrossEncoder
from .io import load_plan, require, read, rows, save, save_rows, complete
from .experiment import embedding_text


def rerank(root, *, base_variant, batch_size=8):
    root=Path(root);load_plan(root);require(root,'retrieve')
    if (root/'rerank-choice.json').exists() and read(root/'rerank-choice.json')['base_variant']!=base_variant:
        raise ValueError('rerank_choice_changed_new_run_required')
    save(root/'rerank-choice.json',dict(base_variant=base_variant,selection='explicit_dev_choice',reserve_candidates=[0,4,8]))
    docs={d['reference']['document_id']:d for d in rows(root/'documents.jsonl')};queries={q['query_id']:q for q in rows(root/'queries.jsonl')}
    model=CrossEncoder(root/'reranker-cache.sqlite3',batch_size=batch_size)
    smokeq='Повышение ключевой ставки: стоимость кредита для предприятия с банковским кредитом.'
    smoke=model.rerank(smokeq,['Банк России повысил ключевую ставку; ставки по кредитам предприятий выросли.','Футбольный клуб выиграл товарищеский матч со счётом 2:0.'])
    save(root/'reranker-smoke.json',dict(query=smokeq,results=smoke,positive_above_negative=smoke[0]['score']>smoke[1]['score'],quality_claim='Russian execution smoke only, not retrieval validation'))
    if not smoke[0]['score']>smoke[1]['score']:raise ValueError('russian_smoke_failed')
    outputs=[];allpaths=[]
    weights=dict(facts=1.,mechanisms=.5,hypotheses=.35,graph=.2)
    for row in rows(root/'retrieval.jsonl'):
        if row['variant']!=base_variant:continue
        paths=[]
        for r in row['candidates'][:100]:
            # One strongest retrieved query per family; don't synthesize unsupported pairs.
            byfamily={}
            for h in r['hits']:
                old=byfamily.get(h['family'])
                if old is None or (h['rank'],h['query_id'])<(old['rank'],old['query_id']):byfamily[h['family']]=h
            for family,h in sorted(byfamily.items()):paths.append(dict(company_id=row['company_id'],document_id=r['document_id'],family=family,query_id=h['query_id']))
        start=time.perf_counter()
        scores=model.score_pairs([(queries[p['query_id']]['text'],embedding_text(docs[p['document_id']])) for p in paths])
        for p,s in zip(paths,scores):p.update(s)
        bydoc=defaultdict(list)
        for p in paths:bydoc[p['document_id']].append(p)
        familyranks={}
        for family in weights:
            ranked=sorted([p for p in paths if p['family']==family and p['status']=='scored'],key=lambda p:(-p['score'],p['document_id']))
            for rank,p in enumerate(ranked,1):familyranks[p['document_id'],family]=rank
        scored=[];unscored=[]
        for r in row['candidates'][:100]:
            r=dict(r);ok=[p for p in bydoc[r['document_id']] if p['status']=='scored']
            if ok:
                best=max(ok,key=lambda p:(p['score'],p['query_id']))
                r.update(reranker_status='scored',best_query_id=best['query_id'],rerank_score=sum(weights[p['family']]/(60+familyranks[r['document_id'],p['family']]) for p in ok))
                scored.append(r)
            else:r.update(reranker_status='unscored',rerank_score=None);unscored.append(r)
        scored.sort(key=lambda r:(-r['rerank_score'],r['document_id']))
        # Unscored occupies its original retrieval position, never disappears below the pool.
        ordered=scored[:]
        for r in sorted(unscored,key=lambda r:r['fusion_rank']):ordered.insert(min(r['fusion_rank']-1,len(ordered)),r)
        for rank,r in enumerate(ordered,1):r['rerank_rank']=rank
        outputs.append(dict(company_id=row['company_id'],variant='G3',base_variant=base_variant,candidates=ordered,seconds=time.perf_counter()-start))
        allpaths.extend(paths)
        save_rows(root/'rerank.partial.jsonl',outputs)
    save_rows(root/'reranking.jsonl',outputs);save_rows(root/'reranker-paths.jsonl',allpaths);save(root/'reranker-profile.json',dict(fingerprint=model.fingerprint,profile=model.profile))
    complete(root,'rerank',[root/'reranking.jsonl',root/'reranker-paths.jsonl',root/'reranker-profile.json',root/'reranker-smoke.json',root/'rerank-choice.json'],model.metrics)
    model.cache.close()
    return model.metrics
