"""Small graph dependency pilot; economic dates never imply availability."""
from contextlib import closing
from dataclasses import replace
import json
import resource
import time
from pathlib import Path
from ranking.profiles import CompanyRepository,file_hash
from ...payment_store import PaymentGraphStore
from ...representation_contracts import PaymentDatasetManifest,BuildConfig,EncoderConfig,GraphPolicy,QueryPolicy
from ...embeddings import Encoder
from ...embedding_cache import VectorCache
from ...company_build import build_company_snapshot,CompanyEmbeddingSnapshot
from ...company_queries import load_company_queries
from .smoke import ROOT,MODEL,save,TIMES,select_ids

def main():
    if json.loads((ROOT/'pilot-report.json').read_text())['status']!='passed':raise ValueError('base pilot not passed')
    started=time.perf_counter();source=Path('data/companies/df_Uid_payment_hack.csv')
    proof=Path('data/task/Новостная аналитика для принятия бизнес-решений Альфа Хакатон.timestamps.txt')
    manifest=PaymentDatasetManifest(str(source),file_hash(source),currency='RUB',period_start='2026-01-01',period_end='2026-10-31',
        metadata_source={'path':str(proof),'sha256':file_hash(proof),'locator':'00:19:47–00:20:47; aggregate period, no available_at supplied'})
    graph_report=PaymentGraphStore.build(source,output_path=ROOT/'payments.sqlite',metadata=manifest)
    print(json.dumps(graph_report.to_dict()),flush=True)
    repo=CompanyRepository('data/companies/df_Uid_data_hack.csv');ids=select_ids(repo,8)
    cfg=EncoderConfig(model_dir=MODEL);encoder=Encoder(cfg)
    with closing(VectorCache(ROOT/'objects.sqlite')) as cache, closing(PaymentGraphStore.open(ROOT/'payments.sqlite')) as graph:
        builds=[];coverage=[]
        for weight in ('equal','amount_share','sqrt_share'):
            config=BuildConfig(str(ROOT/('graph-'+weight)),scope_ids=tuple(ids),encoder_config=cfg,graph_policy=GraphPolicy(enabled=True,weight_policy=weight),include_universal=True,temporal_mode='static_context')
            report=build_company_snapshot(repo,encoder=encoder,cache=cache,config=config,graph_store=graph);builds.append(report.to_dict())
            with closing(CompanyEmbeddingSnapshot.open(config.output_root,cache=cache)) as snap:
                for uid in ids:
                    strict=load_company_queries(snap,company_id=uid,expected_encoder_fingerprint=encoder.fingerprint,policy=QueryPolicy(graph_enabled=True,graph_policy=config.graph_policy),as_of=TIMES['as_of'])
                    if any(q.kind.startswith('graph') for q in strict.queries):raise ValueError('future graph leaked into strict query')
                    if weight=='sqrt_share':
                        views=snap.views(uid,enabled_views=('graph_incoming','graph_outgoing'))
                        coverage.append({'company_id':uid,'directions':{v.direction:v.coverage.to_dict() for v in views}})
            print(json.dumps({'weight':weight,'build':report.to_dict()}),flush=True)
    status=dict(status='passed',payment_store=graph_report.to_dict(),builds=builds,coverage=coverage,temporal_mode='static_context',quality_status='not_evaluated',graph_default_enabled=False,
        strict_graph_omitted=True,elapsed_seconds=time.perf_counter()-started,peak_rss_mib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024)
    save(ROOT/'graph-pilot-report.json',status)
    annotation=ROOT/'evaluation/manifest.json'
    if annotation.exists():
        manifest=json.loads(annotation.read_text())
        with closing(VectorCache(ROOT/'objects.sqlite')) as cache,closing(CompanyEmbeddingSnapshot.open(ROOT/'graph-sqrt_share',cache=cache)) as snapshot:
            contexts={uid:snapshot.company_metadata(uid)['payment_context_ref'] for uid in ids}
            context_file=annotation.parent/'payment_contexts.json'
            judged=any(j.get('status')=='judged' for j in json.loads((annotation.parent/'judgments.json').read_text()))
            if judged and context_file.exists() and json.loads(context_file.read_text())!=contexts:
                raise ValueError('cannot replace annotation context after judgments without a new dataset')
            save(context_file,contexts)
            manifest['files']['payment_contexts']={'path':'payment_contexts.json','sha256':file_hash(annotation.parent/'payment_contexts.json')}
            manifest['context_snapshot_id']=snapshot.snapshot_id;manifest['context_mode']='static_context'
            save(annotation,manifest)
    print(json.dumps(status,indent=2),flush=True)

if __name__=='__main__':main()
