from llm.events.paths import ROOT
"""Explicit v2 planning, bounded execution and A/B evaluation. No network by default."""
import argparse
import json
from pathlib import Path
import llm.events.extraction.staged_models as m
import llm.events.extraction.staged as core
from llm.events.extraction.service import ExtractionService, ExperimentCounter, FormatProbeRunner, read_final, owner_for
from llm.events.extraction.evaluation import compare_variants, export_comparison
from llm.events.extraction.models import ProviderProfile, ExecutionLimits
from llm.events.extraction.admin_cli import read_model, load_cli_env
from llm.events.extraction.provider import OpenAIProvider, OfflineProvider, ProviderResponse
from llm.events.storage.store import EventStore, StorageError, DEFAULT_DB
from llm.events.contracts.validation import ContractValidationError, article_sha256


def schemas():
    return {'span-reference.v1.schema.json':m.SpanRef.model_json_schema(),
        'discovery-span-response.v1.schema.json':core.wire_schema(m.DiscoveryResponse.model_json_schema()),
        'template-span-record.v1.schema.json':core.wire_schema(m.TEMPLATE.json_schema()),
        'verification-span-response.v1.schema.json':core.wire_schema(m.VerificationResponse.model_json_schema()),
        'discovery-response.v2.schema.json':m.DiscoveryResponse.model_json_schema(),
        'discovery-result.v2.schema.json':m.DiscoveryResult.model_json_schema(),
        'extraction-plan.v2.schema.json':m.ExtractionPlan.model_json_schema(),
        'extraction-audit.v2.schema.json':m.ExtractionAudit.model_json_schema(),
        'template-response.v2.schema.json':m.TemplateResponse.model_json_schema(),
        'template-batch.v2.schema.json':m.TemplateBatchResult.model_json_schema(),
        'template-record.v2.schema.json':m.TEMPLATE.json_schema(),
        'semantic-review-response.v1.schema.json':m.VerificationResponse.model_json_schema(),
        'semantic-review-result.v1.schema.json':m.VerificationResult.model_json_schema()}


def select_profile(store,profile,probe_artifact_ids):
    compatible=set()
    if len(probe_artifact_ids)>2:raise StorageError('probe_limit_exceeded','At most two format probes')
    for aid in probe_artifact_ids:
        artifact=store.get_artifact(aid);owner=owner_for(store,artifact);record=store.get_article(owner.source_id)
        read_final(store,aid,record,processor='events.format_probe.v2',kind='format_probe')
        old=ProviderProfile.model_validate(owner.spec.configuration['profile'])
        differences={k for k in type(old).model_fields if getattr(old,k)!=getattr(profile,k)}
        if differences-{'response_format','format_verified'}:
            raise StorageError('provider_profile_mismatch','Probe used another endpoint/model/settings')
        if artifact.payload.get('observed_compatible'):compatible.add(artifact.payload['format'])
    mode=next((x for x in ('json_schema','json_object') if x in compatible),'text')
    return profile.model_copy(update={'response_format':mode,'format_verified':mode!='text'})


def run_experiment(service,article_path,*,max_jobs,gold=None,alignments=None,experiment_id):
    if type(max_jobs) is not int or max_jobs<=0:raise StorageError('invalid_argument','max_jobs must be positive')
    if service.profile.execution_mode=='live' and service.counter is None:
        raise StorageError('experiment_journal_required','Real A/B experiment requires a persistent request-count journal')
    store=service.store;batch=store.import_batch(article_path)
    if batch.import_status=='rejected' or any(item.disposition=='invalid' for item in batch.items):
        raise StorageError('invalid_experiment_input','Resolve invalid input batch before starting calls')
    if len({item.source_id for item in batch.items if item.source_id})>3:
        raise StorageError('experiment_article_limit','First controlled experiment allows at most three articles')
    if gold is not None:
        if gold.get('schema_version')!='extraction-gold.v1' or gold.get('status')!='single_annotator_draft':
            raise StorageError('invalid_gold','Freeze the manual development draft before calls')
        hashes={a['article_id']:a['article_sha256'] for a in gold['articles']}
        for item in batch.items:
            if not item.source_id:continue
            record=store.get_article(item.source_id)
            if hashes.get(record.article.article_id)!=article_sha256(record.article):
                raise StorageError('gold_source_mismatch','Gold differs from experiment article; no calls made')
            for case in gold['cases']:
                if case['article_id']!=record.article.article_id:continue
                e=case['evidence']
                if getattr(record.article,e['field'])[e['start']:e['end']]!=e['quote']:
                    raise StorageError('gold_quote_mismatch','Gold differs from archived text; no calls made')
    rows=[];normal_a=[];normal_b=[]
    stop=False;seen_sources=set()
    # Always finish the A scheduling pass before verification starts.
    for item in batch.items:
        if not item.source_id or item.source_id in seen_sources:continue
        seen_sources.add(item.source_id)
        record=store.get_article(item.source_id);row={'source_id':item.source_id,'article_id':record.article.article_id}
        rows.append(row)
        if stop:row['status']='deferred';continue
        discovery=service.discover(item.source_id);row['discovery']=discovery
        stop=bool(discovery.get('stop'))
        if discovery['status'] not in ('succeeded','cached'):continue
        plan=service.plan_extraction(discovery['artifact_id']);row['plan']=plan
        batches=service.run_extraction(plan['artifact_id'],max_jobs=max_jobs);row['batches']=batches
        stop=stop or any(r.get('stop') for r in batches)
        aids=[r['artifact_id'] for r in batches if r.get('artifact_id') and r['status'] in ('succeeded','cached')]
        assembly=service.assemble_variant(plan['artifact_id'],aids);row['assembly_A']=assembly
        normalized=service.normalize_variant(assembly);row['normalized_A']=normalized
        normal_a.append(normalized['artifact_id'])
    for row in rows:
        normalized=row.get('normalized_A')
        if not normalized or normalized['status'] not in ('succeeded','cached'):continue
        reviews=[] if stop else service.verify_extraction(normalized['artifact_id'],max_jobs=max_jobs)
        row['reviews']=reviews;stop=stop or any(r.get('stop') for r in reviews)
        aid=[r['artifact_id'] for r in reviews if r.get('artifact_id') and r['status'] in ('succeeded','cached')]
        batches=[r['artifact_id'] for r in row['batches'] if r.get('artifact_id') and r['status'] in ('succeeded','cached')]
        assembly=service.assemble_variant(row['plan']['artifact_id'],batches,variant='B',
            normalized_artifact_id=normalized['artifact_id'],verification_artifact_ids=aid)
        row['assembly_B']=assembly;row['normalized_B']=service.normalize_variant(assembly)
        normal_b.append(row['normalized_B']['artifact_id'])
    publications={}
    for variant in ('A','B'):
        normalized=[row['normalized_'+variant]['artifact_id'] for row in rows
            if row.get('normalized_'+variant,{}).get('status') in ('succeeded','cached')]
        publications[variant]=service.publish_variant(variant,normalized,experiment_id=experiment_id)
    calls=service.counter.summary() if service.counter else _standard_calls(store)
    report=compare_variants(store,normal_a,normal_b,gold=gold,alignments=alignments,calls=calls)
    report.update(rows=rows,publications={k:{x:y for x,y in v.items() if x!='bundles'} for k,v in publications.items()})
    return report,calls,{k:v['bundles'] for k,v in publications.items()}


def _standard_calls(store):
    with store._transaction() as c:
        rows=c.execute("SELECT l.request_json,l.response_json,l.created_at,l.finished_at FROM llm_calls l JOIN attempts a ON a.attempt_id=l.attempt_id JOIN jobs j ON j.job_id=a.job_id WHERE json_extract(j.spec_json,'$.processor_name') LIKE 'events.%v2'").fetchall()
    return [{'request':json.loads(r['request_json']),'response':json.loads(r['response_json']) if r['response_json'] else None,
             'elapsed_seconds':max(0,r['finished_at']-r['created_at']) if r['finished_at'] is not None else None} for r in rows]


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--db',type=Path,default=DEFAULT_DB)
    p.add_argument('--profile',type=Path);p.add_argument('--limits',type=Path)
    p.add_argument('--responses',type=Path,help='Scripted offline provider response list')
    p.add_argument('--experimental-credits',action='store_true');p.add_argument('--journal',type=Path)
    p.add_argument('--max-requests',type=int,choices=range(1,61),default=60)
    sub=p.add_subparsers(dest='command',required=True)
    schema=sub.add_parser('schema');schema.add_argument('--check',action='store_true')
    d=sub.add_parser('discover');d.add_argument('--source-id',required=True)
    plan=sub.add_parser('plan');plan.add_argument('--discovery-artifact-id',required=True);plan.add_argument('--save',action='store_true')
    run=sub.add_parser('run');run.add_argument('--plan-artifact-id',required=True);run.add_argument('--max-jobs',type=int,required=True)
    a=sub.add_parser('assemble');a.add_argument('--plan-artifact-id',required=True);a.add_argument('--batch-artifact-ids',nargs='*',default=[])
    a.add_argument('--variant',choices=['A','B'],default='A');a.add_argument('--normalized-A');a.add_argument('--verification-artifact-ids',nargs='*',default=[])
    v=sub.add_parser('verify');v.add_argument('--artifact-id',required=True);v.add_argument('--max-jobs',type=int,required=True)
    pub=sub.add_parser('publish');pub.add_argument('--variant',choices=['A','B'],required=True);pub.add_argument('--experiment-id',required=True)
    pub.add_argument('--artifact-ids',nargs='*',default=[])
    probe=sub.add_parser('probe');probe.add_argument('--source-id',required=True);probe.add_argument('--format',choices=['json_schema','json_object'],required=True)
    selection=sub.add_parser('select-profile');selection.add_argument('--probe-artifact-ids',nargs='*',default=[]);selection.add_argument('--output',type=Path,required=True)
    experiment=sub.add_parser('experiment');experiment.add_argument('--articles',type=Path,required=True)
    experiment.add_argument('--gold',type=Path,required=True);experiment.add_argument('--alignments',type=Path)
    experiment.add_argument('--output',type=Path,required=True);experiment.add_argument('--max-jobs',type=int,required=True)
    experiment.add_argument('--experiment-id',required=True)
    comparison=sub.add_parser('compare');comparison.add_argument('--A',nargs='*',default=[]);comparison.add_argument('--B',nargs='*',default=[])
    comparison.add_argument('--gold',type=Path,required=True);comparison.add_argument('--alignments',type=Path)
    args=p.parse_args(argv)
    try:
        if args.command=='schema':
            for name,schema in schemas().items():
                path=ROOT/'docs/contracts'/name
                schema['$schema']='https://json-schema.org/draft/2020-12/schema'
                if args.check:
                    if not path.exists() or json.loads(path.read_text())!=schema:raise StorageError('schema_drift',name)
                else:path.write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n')
            result={'status':'checked' if args.check else 'generated','schemas':list(schemas())}
        else:
            store=EventStore(args.db)
            if args.command=='compare':
                result=compare_variants(store,args.A,args.B,gold=json.loads(args.gold.read_text()),
                    alignments=json.loads(args.alignments.read_text()) if args.alignments else None)
            else:
                if args.profile is None:raise StorageError('profile_required','Provide --profile')
                profile=read_model(args.profile,ProviderProfile)
                if args.command=='select-profile':
                    result=select_profile(store,profile,args.probe_artifact_ids).model_dump(mode='json')
                    args.output.parent.mkdir(parents=True,exist_ok=True)
                    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
                else:
                    if args.command=='probe':profile=profile.model_copy(update={'response_format':args.format})
                    if args.responses:
                        provider=OfflineProvider([ProviderResponse(**r) for r in json.loads(args.responses.read_text())])
                    else:provider=OpenAIProvider(profile)
                    network=args.command in ('discover','run','verify','experiment','probe') and profile.execution_mode=='live'
                    if network:load_cli_env()
                    counter=None
                    if args.experimental_credits:
                        if args.journal is None:raise StorageError('journal_required','Persistent --journal required for credit mode')
                        counter=ExperimentCounter(args.journal,args.max_requests)
                    limits=read_model(args.limits,ExecutionLimits) if args.limits else ExecutionLimits()
                    service=ExtractionService(store,provider,profile,limits=limits,counter=counter)
                    if args.command=='discover':result=service.discover(args.source_id)
                    elif args.command=='plan':result=service.plan_extraction(args.discovery_artifact_id,persist=args.save)
                    elif args.command=='run':result=service.run_extraction(args.plan_artifact_id,max_jobs=args.max_jobs)
                    elif args.command=='verify':result=service.verify_extraction(args.artifact_id,max_jobs=args.max_jobs)
                    elif args.command=='assemble':
                        result=service.assemble_variant(args.plan_artifact_id,args.batch_artifact_ids,variant=args.variant,
                            normalized_artifact_id=args.normalized_A,verification_artifact_ids=args.verification_artifact_ids)
                        result['normalization']=service.normalize_variant(result)
                    elif args.command=='publish':result=service.publish_variant(args.variant,args.artifact_ids,experiment_id=args.experiment_id)
                    elif args.command=='probe':
                        if counter is None:raise StorageError('credit_journal_required','Capability probes use the experiment journal')
                        result=service._call(store.get_article(args.source_id),FormatProbeRunner(store,provider))
                    else:
                        report,calls,bundles=run_experiment(service,args.articles,max_jobs=args.max_jobs,
                            gold=json.loads(args.gold.read_text()),alignments=json.loads(args.alignments.read_text()) if args.alignments else None,
                            experiment_id=args.experiment_id)
                        result=export_comparison(args.output,report,calls=calls,bundles=bundles,
                            inputs=json.loads(args.articles.read_text()))
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except ContractValidationError as exc:
        print(json.dumps({'errors':[i.model_dump() for i in exc.issues]},ensure_ascii=False,indent=2));return 1

if __name__=='__main__':raise SystemExit(main())
