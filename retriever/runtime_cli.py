"""Explicit immutable index build and one-shot candidate retrieval."""
from contextlib import closing
from pathlib import Path
import json
from ranking.event_store import EventStore
from ranking.profiles import CompanyRepository
from .representation_contracts import EncoderConfig,QueryPolicy,VariantConfig
from .embeddings import Encoder
from .embedding_cache import VectorCache
from .company_build import CompanyEmbeddingSnapshot
from .company_queries import load_company_queries
from .index import build_event_index,EventIndex
from .service import RetrievalService

def run_index(args):
    encoder=Encoder(EncoderConfig(model_dir=args.model_dir))
    with EventStore(args.events) as store,closing(VectorCache(args.cache)) as cache:
        index=build_event_index(store,encoder=encoder,cache=cache,output_path=args.output,as_of=args.as_of,window_start=args.window_start,window_end=args.window_end)
    print(json.dumps({'index_snapshot_id':index.index_snapshot_id,'events':len(index.snapshots),'chunks':len(index.chunks)}))

def run_retrieve(args):
    index=EventIndex.open(args.index);profile=CompanyRepository(args.companies).get(args.company_id)
    times={k:getattr(index,k) for k in ('as_of','window_start','window_end')}
    with closing(VectorCache(args.cache)) as cache,closing(CompanyEmbeddingSnapshot.open(args.snapshot,cache=cache)) as snapshot:
        queries=load_company_queries(snapshot,company_id=args.company_id,expected_encoder_fingerprint=index.fingerprint,policy=QueryPolicy(),as_of=index.as_of)
        config=VariantConfig('H3',role=args.role,token_budget=args.token_budget,**times)
        result=RetrievalService(index).run(dict(request_id=args.request_id,company_id=args.company_id,profile=profile,role=args.role,**times),company_queries=queries,config=config)
    Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'status':result['candidate_batch']['status'],'selected':result['trace']['selected'],'output':args.output}))

def register_parser(subparsers):
    p=subparsers.add_parser('index');p.set_defaults(func=run_index)
    for key in ('events','cache','model-dir','output','as-of','window-start','window-end'):p.add_argument('--'+key,required=True)
    p=subparsers.add_parser('retrieve');p.set_defaults(func=run_retrieve)
    for key in ('index','cache','companies','snapshot','company-id','output'):p.add_argument('--'+key,required=True)
    p.add_argument('--role',choices=['ceo','cfo','treasurer','chief_accountant'],default='cfo');p.add_argument('--request-id',default='cli-request');p.add_argument('--token-budget',type=int,default=8192)
