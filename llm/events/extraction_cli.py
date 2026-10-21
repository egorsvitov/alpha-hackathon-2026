"""B3 administration. Network is opt-in and requires confirmed tariffs and limits."""
import argparse
import json
import os
from pathlib import Path
from pydantic import ValidationError
from .budget import CallLedger, validate_limits, maximum_cost
from .extraction import build_processing_spec, build_request, extraction_schema
from .extraction_models import ProviderProfile, ExecutionLimits
from .migration import migrate_v1_to_v2
from .models import Issue
from .provider import OpenAIProvider, OfflineProvider, ProviderResponse
from .runner import ExtractionRunner
from .storage import EventStore, DEFAULT_DB, StorageError, canonical_json
from .validation import ContractValidationError


def load_cli_env(path=Path('.env')):
    """Read only the literal LLM_API_KEY assignment; never override process env."""
    if 'LLM_API_KEY' in os.environ or not Path(path).is_file():
        return
    for line in Path(path).read_text().splitlines():
        key,sep,value=line.strip().removeprefix('export ').partition('=')
        if sep and key.strip()=='LLM_API_KEY':
            value=value.strip()
            if len(value)>=2 and value[0]==value[-1] and value[0] in {'"',"'"}:
                value=value[1:-1]
            os.environ.setdefault('LLM_API_KEY',value)
            return


def read_model(path,model):
    try:
        return model.model_validate(json.loads(Path(path).read_text()))
    except ValidationError as exc:
        raise StorageError('invalid_configuration','Configuration schema is invalid',issues=[
            Issue(code='invalid_configuration',
                path='/'+ '/'.join(map(str,e['loc'])),message=e['msg']) for e in exc.errors()]) from None


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    p=commands.add_parser('migrate');p.add_argument('--db',type=Path,required=True);p.add_argument('--backup',type=Path)
    p=commands.add_parser('schema');p.add_argument('--check',action='store_true')
    for command in ('validate-profile','prepare','enqueue','run','probe'):
        p=commands.add_parser(command)
        p.add_argument('--profile',type=Path,required=True)
        if command!='validate-profile':p.add_argument('--db',type=Path,default=DEFAULT_DB)
        if command in ('prepare','enqueue','probe'):p.add_argument('--source-id',required=True,action='append' if command in ('enqueue','probe') else 'store')
        if command in ('validate-profile','run','probe'):p.add_argument('--limits',type=Path,required=True)
        if command=='run':
            p.add_argument('--max-jobs',type=int,required=True)
            p.add_argument('--offline-responses',type=Path)
        if command=='probe':p.add_argument('--max-requests',type=int,required=True,choices=range(1,4))
    for command in ('calls','show-call','budget','reconcile','acknowledge-overrun'):
        p=commands.add_parser(command);p.add_argument('--db',type=Path,default=DEFAULT_DB)
        if command in ('show-call','reconcile'):p.add_argument('call_id')
        if command=='reconcile':p.add_argument('--actual-rub',required=True)
        if command in ('reconcile','acknowledge-overrun'):p.add_argument('--reason',required=True)
        if command=='acknowledge-overrun':p.add_argument('--day',required=True)
        if command=='budget':p.add_argument('--offline',action='store_true');p.add_argument('--day')
    args=parser.parse_args(argv)
    try:
        if args.command=='migrate':
            result=migrate_v1_to_v2(args.db,args.backup)
        elif args.command=='schema':
            path=Path(__file__).resolve().parents[2]/'docs/contracts/extraction-response.b3.1.schema.json'
            schema=extraction_schema();schema['$schema']='https://json-schema.org/draft/2020-12/schema'
            if args.check:
                if not path.exists() or json.loads(path.read_text())!=schema:
                    raise StorageError('schema_drift','B3 schema differs from models')
            else:
                path.write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n')
            result={'schema':str(path),'status':'checked' if args.check else 'generated'}
        elif args.command in ('calls','show-call','budget','reconcile','acknowledge-overrun'):
            ledger=CallLedger(EventStore(args.db),offline=getattr(args,'offline',False))
            if args.command=='calls':result=ledger.list_calls()
            elif args.command=='show-call':result=ledger.get(args.call_id)
            elif args.command=='budget':result=ledger.budget(args.day)
            elif args.command=='reconcile':result=ledger.reconcile(args.call_id,args.actual_rub,args.reason)
            else:result=ledger.acknowledge_overrun(args.day,args.reason)
        else:
            profile=read_model(args.profile,ProviderProfile)
            spec=build_processing_spec(profile)
            if args.command=='validate-profile':
                limits=read_model(args.limits,ExecutionLimits)
                validate_limits(profile,limits,live=profile.execution_mode=='live')
                result={'status':'ready','spec':spec.model_dump(mode='json'),'maximum_call_rub':str(maximum_cost(profile,limits))}
            else:
                store=EventStore(args.db)
                if args.command=='prepare':
                    request=build_request(store.get_article(args.source_id),profile)
                    size=len(canonical_json(request).encode())
                    result={'spec':spec.model_dump(mode='json'),'request':request,'request_bytes':size,'within_size_limit':size<=profile.max_request_bytes}
                elif args.command=='enqueue':
                    result=[store.enqueue(sid,spec).model_dump(mode='json') for sid in args.source_id]
                else:
                    limits=read_model(args.limits,ExecutionLimits)
                    if args.command=='run' and args.offline_responses:
                        raw=json.loads(args.offline_responses.read_text())
                        provider=OfflineProvider([ProviderResponse(**r) for r in raw])
                    else:
                        load_cli_env()
                        provider=OpenAIProvider(profile)
                    runner=ExtractionRunner(store,provider)
                    if args.command=='run':
                        result=[r.model_dump(mode='json') for r in runner.run_pending(spec,limits,args.max_jobs)]
                    else:
                        if profile.execution_mode!='live' or len(args.source_id)>args.max_requests:
                            raise StorageError('invalid_probe','Probe requires live mode and at most max_requests source IDs')
                        # Check gates before enqueueing any probe jobs. Only the
                        # capability-verification flags may be bypassed here.
                        runner._preflight(spec,limits,probe=True)
                        result=[]
                        for sid in dict.fromkeys(args.source_id):
                            job=store.enqueue(sid,spec)
                            if job.state=='succeeded':
                                result.append({'status':'cached','job_id':job.job_id})
                                continue
                            if job.state!='pending':
                                result.append({'status':'deferred','job_id':job.job_id,
                                    'reason':'job_already_running' if job.state=='running' else 'explicit_retry_required'})
                                continue
                            outcome=runner.run_next(spec,limits,_probe=True,source_id=sid)
                            result.append(outcome.model_dump(mode='json'))
                            if outcome.stop:break
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if isinstance(result,list) and any(r.get('status') in {'failed','deferred','superseded'} for r in result if isinstance(r,dict)):
            return 1
        return 0
    except ContractValidationError as exc:
        print(json.dumps({'status':'error','issues':[i.model_dump() for i in exc.issues]},ensure_ascii=False,indent=2))
        return 1
    except (OSError,ValueError,TypeError):
        # Do not echo config contents or a possible credential in an exception.
        print(json.dumps({'status':'error','issues':[{'code':'invalid_input','path':'/','message':'Unable to read or parse input'}]}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
