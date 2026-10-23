"""Authorized local CPU integration gates. Artifacts stay under ignored data/processed."""
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
import argparse
import json
import resource
import time
from ranking.profiles import CompanyRepository,file_hash
from ranking.contracts import payload_hash
from ranking.event_store import EventStore
from ...embeddings import Encoder
from ...embedding_cache import VectorCache
from ...company_build import build_company_snapshot,CompanyEmbeddingSnapshot
from ...representation_contracts import BuildConfig,EncoderConfig,QueryPolicy,VariantConfig
from ...index import build_event_index,EventIndex
from ...company_queries import load_company_queries
from ...service import RetrievalService

ROOT=Path('data/processed/retriever/company-embeddings')
MODEL='ranking/experiments/cpu_news_embeddings/artifacts/model'
CORPUS=Path('ranking/experiments/cpu_news_embeddings/artifacts')
TIMES=dict(as_of='2026-10-22T00:00:00Z',window_start='2026-10-20T00:00:00Z',window_end='2026-10-22T00:00:00Z')

def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def select_ids(repo,limit):
    # Select by source attributes before seeing retrieval results.
    result=[]
    predicates=[lambda r:r['MAINOKVED_CODE'].startswith('49.'),lambda r:r['MAINOKVED_CODE'].startswith('51.'),
        lambda r:r['MAINOKVED_CODE'].startswith('62.'),lambda r:r['MAINOKVED_CODE'].startswith('46.') and r['FTS_FLAG']=='1.0',
        lambda r:r['MAINOKVED_CODE'].startswith('46.') and r['FTS_FLAG']=='0.0',lambda r:r['MAINOKVED_CODE'].startswith('41.'),
        lambda r:r['MAINOKVED_CODE'].startswith('10.'),lambda r:not r['MAINOKVED_NAME']]
    valid=sorted(repo.company_ids-repo.conflicts)
    for pred in predicates:
        candidate=next((uid for uid in valid if uid not in result and pred(repo.rows[uid][0])),None)
        if candidate:result.append(candidate)
    # Include industry diversity for the 256-company pilot.
    used={repo.rows[uid][0]['MAINOKVED_CODE'] for uid in result}
    for uid in valid:
        code=repo.rows[uid][0]['MAINOKVED_CODE']
        if code not in used:result.append(uid);used.add(code)
        if len(result)>=limit:break
    return result[:limit]

def run(stage):
    started=time.perf_counter();ROOT.mkdir(parents=True,exist_ok=True)
    gate=ROOT/('smoke-report.json' if stage=='pilot' else 'pilot-report.json')
    if stage=='full' and (not (ROOT/'graph-pilot-report.json').exists() or json.loads((ROOT/'graph-pilot-report.json').read_text())['status']!='passed'):
        raise ValueError('graph dependency pilot gate not passed')
    if stage!='smoke' and (not gate.exists() or json.loads(gate.read_text())['status']!='passed'):raise ValueError('previous gate not passed')
    cfg=EncoderConfig(model_dir=MODEL)
    with ExitStack() as resources:
        repo=CompanyRepository('data/companies/df_Uid_data_hack.csv')
        encoder=Encoder(cfg);cache=resources.enter_context(closing(VectorCache(ROOT/'objects.sqlite')))
        ids=select_ids(repo,8 if stage=='smoke' else 256) if stage!='full' else sorted(repo.company_ids-repo.conflicts)
        buildcfg=BuildConfig(str(ROOT/stage),scope_ids=tuple(ids),encoder_config=cfg,include_universal=stage!='full')
        report=build_company_snapshot(repo,encoder=encoder,cache=cache,config=buildcfg)
        print(json.dumps({'stage':stage,'build':report.to_dict()},ensure_ascii=False),flush=True)
        warm=build_company_snapshot(repo,encoder=encoder,cache=cache,config=buildcfg)
        if warm.statistics['encoded_texts']!=0 or warm.snapshot_id!=report.snapshot_id:raise ValueError('cache repeat gate failed')
        snapshot=resources.enter_context(closing(CompanyEmbeddingSnapshot.open(ROOT/stage,cache=cache)))
        status=dict(status='passed',stage=stage,build=report.to_dict(),warm=warm.to_dict(),encoder_fingerprint=encoder.fingerprint,
            model=encoder.fingerprint_metadata,source_sha256=repo.sha256,company_count=len(ids),quality_status='not_evaluated',
            graph_default_enabled=False,paid_llm_calls=0,limitations=['profile_data_as_of_unknown','unlabelled_runtime_smoke_not_quality_evaluation'])
        if stage=='smoke':
            collection=json.loads((CORPUS/'collection.json').read_text());bundles=[json.loads(line) for line in (CORPUS/'events.v0.2.jsonl').read_text().splitlines()]
            store=resources.enter_context(EventStore(ROOT/'events.sqlite'))
            for b in bundles:store.put(b,received_at=collection['collected_at'],source_mode='fixture')
            index_start=time.perf_counter()
            index=build_event_index(store,encoder=encoder,cache=cache,output_path=ROOT/'event-index',**TIMES)
            status.update(index_seconds=time.perf_counter()-index_start,event_count=len(index.snapshots),chunk_count=len(index.chunks),index_snapshot_id=index.index_snapshot_id)
            service=RetrievalService(index);results=[];profiles=[];queries=[];judgments=[]
            for uid in ids:
                profile=repo.get(uid);profiles.append(profile)
                request=dict(request_id='smoke:'+uid,company_id=uid,role='cfo',**TIMES)
                queries.append(request)
                vectors=load_company_queries(snapshot,company_id=uid,expected_encoder_fingerprint=index.fingerprint,policy=QueryPolicy(),as_of=TIMES['as_of'])
                result=service.run(dict(request,profile=profile),company_queries=vectors,config=VariantConfig('H3',**TIMES))
                save(ROOT/'smoke-results'/f'{uid}.json',result)
                results.append(dict(company_id=uid,okved=profile['okved'],status=result['candidate_batch']['status'],selected=result['trace']['selected'],token_estimate=result['trace']['token_estimate'],latency_seconds=result['trace']['duration_seconds'],context_overflow=result['trace']['context_overflow']))
                for b in bundles:
                    e=b['event'];judgments.append(dict(company_id=uid,company_context_hash=payload_hash(profile),role='cfo',event_id=e['event_id'],event_version=e['event_version'],payload_hash=payload_hash(b),story_group_id=e['event_id'],label=None,status='not_judged',label_source='human',annotator=None,annotation_version='pilot-form.v1',basis_feature_refs=[],basis_claim_refs=[],assumptions=[],synthetic=False))
            status['queries']=results
            data=ROOT/'evaluation';data.mkdir(exist_ok=True)
            existing=data/'judgments.json'
            if existing.exists() and any(j.get('status')=='judged' for j in json.loads(existing.read_text())):
                raise ValueError('refusing to overwrite completed human/weak judgments; use a new evaluation directory')
            for name,value in [('profiles',profiles),('events',bundles),('queries',queries),('judgments',judgments)]:save(data/f'{name}.json',value)
            manifest=dict(role='cfo',split_status='diagnostic_only_story_groups_need_human_review',splits={'diagnostic':[b['event']['event_id'] for b in bundles]},story_groups={b['event']['event_id']:b['event']['event_id'] for b in bundles},
                files={name:{'path':name+'.json','sha256':file_hash(data/f'{name}.json')} for name in ('profiles','events','queries','judgments')},source_corpus_sha256=file_hash(CORPUS/'events.v0.2.jsonl'),company_snapshot_id=snapshot.snapshot_id,index_snapshot_id=index.index_snapshot_id,**TIMES)
            save(data/'manifest.json',manifest)
            if not all(r['status']=='ready' and r['selected']>0 and not r['context_overflow'] for r in results):status['status']='failed'
        status['peak_rss_mib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
        status['elapsed_seconds']=time.perf_counter()-started
        status['artifact_bytes']=sum(p.stat().st_size for p in ROOT.rglob('*') if p.is_file())
        save(ROOT/(stage+'-report.json'),status)
        print(json.dumps(status,ensure_ascii=False,indent=2),flush=True)
    return 0 if status['status']=='passed' else 1

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['smoke','pilot','full']);args=parser.parse_args();raise SystemExit(run(args.stage))
