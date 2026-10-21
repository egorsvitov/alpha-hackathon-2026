"""B5 catalog snapshots, review and explicit provider advice."""
import argparse
import json
from pathlib import Path
import sqlite3
from .catalog_models import CatalogConfig, CatalogSnapshot, IdentityDescriptor, PairCheckResult, ReviewRequest
from .catalog_store import CatalogService
from .catalog_checks import check_fixtures, evaluate_gold
from .extraction_cli import load_cli_env, read_model
from .extraction_models import ProviderProfile, ExecutionLimits
from .migration import migrate_v1_to_v2, migrate_v2_to_v3
from .provider import OfflineProvider, ProviderResponse
from .storage import EventStore, DEFAULT_DB, StorageError
from .validation import ContractValidationError


def schemas():
    return {'event-catalog.b5.1.schema.json':CatalogSnapshot.model_json_schema(),
            'event-identity.b5.1.schema.json':IdentityDescriptor.model_json_schema(),
            'pair-check.b5.1.schema.json':PairCheckResult.model_json_schema()}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=DEFAULT_DB)
    parser.add_argument('--catalog',default='default')
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('schema');p.add_argument('--check',action='store_true')
    p=commands.add_parser('check');p.add_argument('--inputs',type=Path,default=Path(__file__).parent/'fixtures/catalog.b5.1.json')
    p=commands.add_parser('evaluate');p.add_argument('--gold',type=Path,required=True);p.add_argument('--articles',type=Path)
    p=commands.add_parser('migrate');p.add_argument('--to',type=int,choices=(2,3),default=3);p.add_argument('--backup',type=Path)
    for name in ('init','rebuild'):
        p=commands.add_parser(name);p.add_argument('--config',type=Path)
    for name in ('plan','apply'):
        p=commands.add_parser(name);p.add_argument('--artifact-id',action='append',default=[]);p.add_argument('--replace',action='store_true')
    for name in ('events','pairs'):
        p=commands.add_parser(name);p.add_argument('--revision',type=int)
        if name=='events':p.add_argument('--include-withdrawn',action='store_true')
        else:p.add_argument('--uncertain-only',action='store_true')
    p=commands.add_parser('event');p.add_argument('event_id');p.add_argument('--version',type=int);p.add_argument('--revision',type=int)
    p=commands.add_parser('changes');p.add_argument('--since',type=int,required=True)
    commands.add_parser('reviews')
    p=commands.add_parser('review');p.add_argument('--request',type=Path,required=True)
    p=commands.add_parser('revoke');p.add_argument('review_id');p.add_argument('--reason',required=True)
    p=commands.add_parser('show-recommendation');p.add_argument('artifact_id')
    p=commands.add_parser('check-pair');p.add_argument('--left',required=True);p.add_argument('--right',required=True)
    p.add_argument('--profile',type=Path,required=True);p.add_argument('--limits',type=Path,required=True)
    p.add_argument('--offline-responses',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command=='schema':
            for name,schema in schemas().items():
                schema['$schema']='https://json-schema.org/draft/2020-12/schema'
                path=Path(__file__).resolve().parents[2]/'docs/contracts'/name
                if args.check:
                    if not path.exists() or json.loads(path.read_text())!=schema:raise StorageError('schema_drift',name)
                else:path.write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n')
            result={'status':'checked' if args.check else 'generated'}
        elif args.command=='check':result=check_fixtures(args.inputs)
        elif args.command=='evaluate':result=evaluate_gold(args.gold,args.articles)
        elif args.command=='migrate':
            if not args.db.is_file():raise StorageError('database_not_found','An existing database is required')
            conn=sqlite3.connect(args.db)
            try:version=conn.execute('PRAGMA user_version').fetchone()[0]
            finally:conn.close()
            steps=[]
            if version==1:
                steps.append(migrate_v1_to_v2(args.db,args.backup));version=2
            if args.to==3 and version==2:steps.append(migrate_v2_to_v3(args.db,args.backup if not steps else None))
            if not steps:raise StorageError('unsupported_storage_schema','No supported migration step was selected')
            result={'status':'migrated','steps':steps}
        else:
            service=CatalogService(EventStore(args.db),args.catalog)
            if args.command in ('init','rebuild'):
                config=read_model(args.config,CatalogConfig) if args.config else None
                result=service.create_catalog(config) if args.command=='init' else service.rebuild_catalog(config)
            elif args.command in ('plan','apply'):
                fn=service.plan_catalog_update if args.command=='plan' else service.apply_artifacts
                result=fn(args.artifact_id,replace=args.replace)
            elif args.command=='events':result=[r.model_dump(mode='json') for r in service.list_events(revision=args.revision,include_withdrawn=args.include_withdrawn)]
            elif args.command=='event':result=service.get_event(args.event_id,event_version=args.version,revision=args.revision).model_dump(mode='json')
            elif args.command=='pairs':result=[d.model_dump(mode='json') for d in service.list_decisions(revision=args.revision,uncertain_only=args.uncertain_only)]
            elif args.command=='changes':result=service.changes_since(args.since)
            elif args.command=='reviews':result=service.list_reviews()
            elif args.command=='review':result=service.review_pair(read_model(args.request,ReviewRequest))
            elif args.command=='revoke':result=service.revoke_review(args.review_id,args.reason)
            elif args.command=='show-recommendation':result=service.get_recommendation(args.artifact_id)
            else:
                profile=read_model(args.profile,ProviderProfile);limits=read_model(args.limits,ExecutionLimits)
                if args.offline_responses:provider=OfflineProvider([ProviderResponse(**r) for r in json.loads(args.offline_responses.read_text())])
                else:load_cli_env();provider=None
                result=service.check_pair(args.left,args.right,profile,limits,provider=provider)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if isinstance(result,dict) and result.get('status') in ('failed','deferred','superseded','running') else 0
    except ContractValidationError as exc:
        print(json.dumps({'status':'error','issues':[i.model_dump() for i in exc.issues]},ensure_ascii=False,indent=2));return 1
    except (OSError,ValueError,TypeError):
        print(json.dumps({'status':'error','issues':[{'code':'invalid_input','path':'/','message':'Unable to read or parse input'}]}));return 1


if __name__=='__main__':raise SystemExit(main())
