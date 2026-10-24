"""Run from repo root: PYTHONPATH=backend python -m app.full_cycle --help."""
import argparse
import json
import os
import time
from pathlib import Path

from .storage import Workspace, PipelineError, now
from .profiles import ProfileStore
from .ingest import ingest
from .jobs import run_job
from .runtime import analyst_runtime, merge_runtime


def encoder(args,workspace):
    if not args.model_path:
        return None
    if not args.encoder_reference:
        raise PipelineError('encoder_reference_required')
    from .runtime import LazyCPUEncoder
    return LazyCPUEncoder(args.encoder_reference,args.model_path,workspace.root/'encoder',
        fingerprint=args.fingerprint,dimension=args.dimension,threads=args.threads)


def summary(job):
    return {k:job.get(k) for k in ('id','status','error_code','counts','costs','seconds','reused')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',default=os.environ.get('FULL_CYCLE_DATA','/data/full-cycle'))
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('status')
    demo = sub.add_parser('synthetic',help='Explicit synthetic corpus + fake encoder/LLM; no network')
    worker = sub.add_parser('worker',help='Ingest collector file then analyze selected company/role')
    worker.add_argument('--corpus',required=True)
    worker.add_argument('--profiles',default=os.environ.get('FULL_CYCLE_PROFILES'))
    worker.add_argument('--company',action='append',required=True)
    worker.add_argument('--role',action='append',choices=['ceo','cfo'])
    worker.add_argument('--as-of',required=True,help='Explicit ISO timestamp with offset; part of idempotency key')
    worker.add_argument('--k',type=int,default=20)
    worker.add_argument('--vectors',help='Native bundle; fingerprint is still pinned separately')
    worker.add_argument('--clusters',help='Validated ClusterSnapshot JSON/JSONL; otherwise existing complete-link grouping')
    worker.add_argument('--corpus-id',default='collector-v1')
    worker.add_argument('--fingerprint',required=True)
    worker.add_argument('--dimension',type=int,default=1024)
    worker.add_argument('--grouping-threshold',type=float,default=0.75)
    worker.add_argument('--model-path',help='Local pinned BGE-M3 weights; no download')
    worker.add_argument('--encoder-reference',help='BGE reference vectors/profile for verified CPU parity')
    worker.add_argument('--threads',type=int,default=2)
    worker.add_argument('--raw-fallback',action='store_true')
    worker.add_argument('--include-graph',action='store_true')
    worker.add_argument('--live',action='store_true')
    worker.add_argument('--shared-quota-db')
    worker.add_argument('--shared-call-limit',type=int)
    worker.add_argument('--budget-rub',type=float)
    worker.add_argument('--merge-calls',type=int,default=0)
    worker.add_argument('--retry-failed',action='store_true',help='Explicit retry after external outcome reconciliation')
    worker.add_argument('--every',type=int,default=0,help='Watch collector file; 0 = one pass')
    args = parser.parse_args()
    workspace = Workspace(args.data)
    os.umask(0o077)
    if args.command == 'status':
        from .api import SavedDataStore
        print(json.dumps(SavedDataStore(args.data).readiness(),ensure_ascii=False))
        return 0
    if args.command == 'synthetic':
        from .synthetic import run
        print(json.dumps(run(workspace),ensure_ascii=False))
        return 0
    if args.merge_calls and not args.live:
        parser.error('--merge-calls requires --live (offline merge only uses cached results)')
    try:
        analyst, quota = analyst_runtime(workspace,live=args.live,quota_path=args.shared_quota_db,
                                         quota_limit=args.shared_call_limit,budget_rub=args.budget_rub)
        merge = merge_runtime(workspace,live=args.live,quota=quota)
        enc = None  # Do not load model on unchanged native-bundle iterations.
        while True:
            workspace.set_state('worker',dict(status='running',heartbeat=now()))
            try:
                if args.model_path and enc is None:
                    enc = encoder(args,workspace)
                imported = ingest(workspace,args.corpus,encoder=enc,vectors=args.vectors,clusters=args.clusters,
                    expected_fingerprint=args.fingerprint,expected_dimension=args.dimension,
                    grouping_threshold=args.grouping_threshold,corpus_id=args.corpus_id)
                profiles = ProfileStore(args.profiles or workspace.root/'companies',workspace)
                jobs = [summary(run_job(workspace,profiles,c,r,args.as_of,analyst=analyst,encoder=enc,merge=merge,
                    k=args.k,raw_fallback=args.raw_fallback,include_graph=args.include_graph,
                    max_merge_calls=args.merge_calls,retry_failed=args.retry_failed))
                    for c in args.company for r in (args.role or ['ceo','cfo'])]
                status = 'failed' if any(j['status']=='failed' for j in jobs) else 'ready'
                workspace.set_state('worker',dict(status=status,heartbeat=now(),jobs=jobs))
                print(json.dumps(dict(ingestion={k:imported.get(k) for k in ('changed','article_count','new_articles','group_count','new_embeddings','seconds')},jobs=jobs),ensure_ascii=False),flush=True)
                if not args.every:
                    return 1 if status=='failed' else 0
            except Exception as exc:
                code = exc.code if isinstance(exc,PipelineError) else type(exc).__name__
                workspace.set_state('worker',dict(status='failed',code=code,heartbeat=now()))
                print(json.dumps(dict(status='failed',code=code)),flush=True)
                if not args.every:
                    return 1
            time.sleep(max(1,args.every))
    except PipelineError as exc:
        print(json.dumps(dict(status='failed',code=exc.code)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
