"""B4 normalization and explicit repair; no implicit model calls."""
import argparse
import json
from pathlib import Path
from .extraction_cli import read_model, load_cli_env
from .extraction_models import ProviderProfile, ExecutionLimits
from .normalization import normalization_schema
from .normalization_models import NormalizationConfig
from .normalization_store import NormalizationService
from .normalization_checks import check_inputs
from .provider import OfflineProvider, ProviderResponse
from .storage import EventStore, DEFAULT_DB, StorageError
from .validation import ContractValidationError


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=DEFAULT_DB)
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('schema');p.add_argument('--check',action='store_true')
    p=commands.add_parser('check');p.add_argument('--inputs',type=Path,default=Path(__file__).parent/'fixtures/normalization.b4.1.json');p.add_argument('--articles',type=Path)
    for command in ('normalize','batch','repair'):
        p=commands.add_parser(command);p.add_argument('--config',type=Path)
        if command in ('normalize','batch'):p.add_argument('--artifact-id',required=True,action='append' if command=='batch' else 'store')
        if command=='batch':p.add_argument('--max-jobs',type=int,required=True)
        if command=='repair':
            group=p.add_mutually_exclusive_group(required=True)
            group.add_argument('--artifact-id');group.add_argument('--call-id')
            p.add_argument('--profile',type=Path,required=True);p.add_argument('--limits',type=Path,required=True)
            p.add_argument('--offline-responses',type=Path)
    p=commands.add_parser('show');p.add_argument('artifact_id')
    args=parser.parse_args(argv)
    try:
        if args.command=='check':
            result=check_inputs(args.inputs,args.articles)
        elif args.command=='schema':
            path=Path(__file__).resolve().parents[2]/'docs/contracts/normalized-article.b4.1.schema.json'
            schema=normalization_schema();schema['$schema']='https://json-schema.org/draft/2020-12/schema'
            if args.check:
                if not path.exists() or json.loads(path.read_text())!=schema:raise StorageError('schema_drift','B4 schema differs from models')
            else:path.write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n')
            result={'status':'checked' if args.check else 'generated','schema':str(path)}
        else:
            store=EventStore(args.db);service=NormalizationService(store)
            config=read_model(args.config,NormalizationConfig) if getattr(args,'config',None) else NormalizationConfig()
            if args.command=='normalize':result=service.normalize_artifact(args.artifact_id,config)
            elif args.command=='batch':result=service.normalize_batch(args.artifact_id,config,args.max_jobs)
            elif args.command=='show':result=store.get_artifact(args.artifact_id).model_dump(mode='json')
            else:
                profile=read_model(args.profile,ProviderProfile);limits=read_model(args.limits,ExecutionLimits)
                provider=None
                if args.offline_responses:
                    provider=OfflineProvider([ProviderResponse(**r) for r in json.loads(args.offline_responses.read_text())])
                else:load_cli_env()
                ref={'artifact_id':args.artifact_id} if args.artifact_id else {'call_id':args.call_id}
                result=service.repair_input(ref,profile,limits,config=config,provider=provider)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        outcomes=result if isinstance(result,list) else [result]
        bad={'failed','deferred','superseded','running'}
        return 1 if any(r.get('status') in bad or r.get('normalization',{}).get('status') in bad for r in outcomes) else 0
    except ContractValidationError as exc:
        print(json.dumps({'status':'error','issues':[i.model_dump() for i in exc.issues]},ensure_ascii=False,indent=2));return 1
    except (OSError,ValueError,TypeError):
        print(json.dumps({'status':'error','issues':[{'code':'invalid_input','path':'/','message':'Unable to read or parse input'}]}));return 1


if __name__=='__main__':raise SystemExit(main())
