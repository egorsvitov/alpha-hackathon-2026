"""Offline company snapshot build, resume, verify, inspect and portable export."""
from pathlib import Path
from contextlib import ExitStack, closing
import json
import shutil
import tempfile
from .representation_contracts import BuildConfig, EncoderConfig, GraphPolicy, PaymentDatasetManifest
from .embedding_cache import VectorCache
from .company_build import CompanyEmbeddingSnapshot, build_company_snapshot


def export_snapshot(snapshot, *, destination):
    destination=Path(destination)
    if destination.exists():raise FileExistsError(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent,prefix='.export-') as name:
        stage=Path(name)
        shutil.copytree(snapshot.path,stage/'snapshot')
        cache=VectorCache(stage/'objects.sqlite')
        try:
            with cache.writer_lock():
                for key in snapshot.manifest['objects']:
                    cache.put(key,snapshot.cache.get(key),metadata=snapshot.cache.metadata(key))
            manifest=json.loads((stage/'snapshot/manifest.json').read_text())
            manifest['storage_locator']='../objects.sqlite'
            (stage/'snapshot/manifest.json').write_text(json.dumps(manifest,sort_keys=True)+'\n')
            check=CompanyEmbeddingSnapshot.open(stage/'snapshot',cache=cache);check.close()
        finally:cache.close()
        stage.rename(destination)
    return {'path':str(destination),'objects':len(snapshot.manifest['objects'])}


def execute(args):
    with ExitStack() as stack:
        cache=stack.enter_context(closing(VectorCache(args.cache)))
        if args.action in {'build','resume'}:
            from ranking.profiles import CompanyRepository
            from .embeddings import Encoder
            from .payment_store import PaymentGraphStore
            if args.dry_run:
                repo=CompanyRepository(args.companies)
                valid=repo.company_ids-repo.conflicts
                scope=set(args.ids) if args.ids else valid
                if not scope<=valid:raise ValueError('unknown/conflicting target IDs')
                print(json.dumps({'dry_run':True,'target_companies':len(scope),'graph_enabled':bool(args.graph),'model_inference':False}))
                return
            cfg=EncoderConfig(threads=args.threads,model_dir=args.model_dir)
            encoder=Encoder(cfg)
            repo=CompanyRepository(args.companies)
            graph=stack.enter_context(closing(PaymentGraphStore.open(args.graph))) if args.graph else None
            config=BuildConfig(args.output,scope_ids=tuple(args.ids) if args.ids else None,
                graph_policy=GraphPolicy(enabled=bool(graph),weight_policy=args.weights),encoder_config=cfg,
                include_universal=args.universal,temporal_mode=args.temporal_mode)
            result=build_company_snapshot(repo,encoder=encoder,cache=cache,graph_store=graph,config=config,resume=args.action=='resume').to_dict()
        else:
            snapshot=stack.enter_context(closing(CompanyEmbeddingSnapshot.open(args.snapshot,cache=cache)))
            if args.action=='verify':result=snapshot.verify().to_dict()
            elif args.action=='inspect':
                result={'metadata':snapshot.company_metadata(args.company_id),'views':[v.to_dict() for v in snapshot.views(args.company_id,enabled_views=('business','finance','trade_geo','universal','graph_incoming','graph_outgoing'))]}
            else:result=export_snapshot(snapshot,destination=args.destination)
    print(json.dumps(result,ensure_ascii=False,indent=2))


def register_parser(subparsers):
    parser=subparsers.add_parser('company-embeddings')
    actions=parser.add_subparsers(dest='action',required=True)
    for action in ('build','resume','verify','inspect','export'):
        p=actions.add_parser(action);p.add_argument('--cache',required=True);p.set_defaults(func=execute)
        if action in {'build','resume'}:
            p.add_argument('--dry-run',action='store_true');p.add_argument('--companies',required=True);p.add_argument('--output',required=True);p.add_argument('--ids',nargs='+')
            p.add_argument('--graph');p.add_argument('--weights',choices=['equal','amount_share','sqrt_share'],default='sqrt_share')
            p.add_argument('--threads',type=int,default=2);p.add_argument('--model-dir');p.add_argument('--universal',action='store_true')
            p.add_argument('--temporal-mode',choices=['strict','static_context'],default='strict')
        else:
            p.add_argument('--snapshot',required=True)
            if action=='inspect':p.add_argument('--company-id',required=True)
            if action=='export':p.add_argument('--destination',required=True)
