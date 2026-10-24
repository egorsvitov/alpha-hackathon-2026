"""Local import, validation and retrieval. Network requires explicit CLI budgets."""
import argparse
import importlib
import json
from pathlib import Path
import sqlite3
import sys

from pydantic import ValidationError

from .importers import read_articles, read_clusters, read_interests, read_vector_bundle
from .models import RetrievalRequest
from .search import exact_search
from .output import write_run
from .selection import RetrieverService
from .store import SQLiteSearchStore


class _Offline:
    def reserve(self, key):
        raise RuntimeError('offline_new_calls_disabled')

    def complete(self, request, profile):
        raise RuntimeError('offline_provider_disabled')


def _parser():
    parser = argparse.ArgumentParser(prog='python -m retriever', description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('import', 'validate'):
        command = commands.add_parser(name, help='Import inputs transactionally' if name == 'import' else 'Validate inputs or an existing index')
        command.add_argument('--articles')
        command.add_argument('--vectors', help='Native vector bundle manifest or directory')
        command.add_argument('--clusters')
        command.add_argument('--store', required=name == 'import', help='SQLite index path')
    inspect = commands.add_parser('inspect', help='Print index metadata and counts')
    inspect.add_argument('--store', required=True)
    retrieve = commands.add_parser('retrieve', help='Write batch.json and trace.json locally')
    retrieve.add_argument('--store', required=True)
    retrieve.add_argument('--request', required=True)
    retrieve.add_argument('--interests', required=True)
    retrieve.add_argument('--output', required=True, help='Local output directory')
    retrieve.add_argument('--encoder-factory', help='Trusted local module:function, called with no arguments')
    retrieve.add_argument('--merge-state', help='Existing merge cache, or state path in explicit live mode')
    retrieve.add_argument('--profile', help='Merge Profile JSON; same profile required for cache reuse')
    retrieve.add_argument('--max-calls', type=int, help='Explicit new-call limit; default offline')
    retrieve.add_argument('--quota-db', help='Existing initialized shared quota SQLite file')
    retrieve.add_argument('--quota-limit', type=int)
    return parser


def _existing(path, name):
    if not path or not Path(path).is_file():
        raise ValueError(f'{name}_must_be_existing_file')
    return path


def _inputs(args):
    if not args.articles or not args.vectors:
        raise ValueError('articles_and_vectors_required_together')
    articles, diagnostics = read_articles(args.articles)
    bundle = read_vector_bundle(args.vectors, articles)
    clusters, cluster_diagnostics = read_clusters(args.clusters, articles) if args.clusters else ([], [])
    return dict(articles=articles, clusters=clusters, **bundle), diagnostics + cluster_diagnostics


def _summary(snapshot):
    return dict(corpus_id=snapshot.corpus_id, index_generation=snapshot.generation,
                encoder_fingerprint=snapshot.encoder_fingerprint, dimension=snapshot.dimension,
                active_items=len(snapshot.items), vector_rows=len(snapshot.vectors))


def _merge(args, request):
    from llm.cluster_merge.models import Profile
    from llm.cluster_merge.provider import OpenAIProvider, SQLiteQuota
    from llm.cluster_merge.service import MergeService
    from .merge_adapter import ClusterMergeAdapter

    calls = 0 if args.max_calls is None else args.max_calls
    if calls < 0:
        raise ValueError('max_calls_must_be_nonnegative')
    if args.max_calls is None and request.max_new_llm_calls:
        raise ValueError('positive_request_budget_requires_explicit_max_calls')
    if args.max_calls is not None:
        request = request.model_copy(update={'max_new_llm_calls': calls})
    profile = Profile.model_validate_json(Path(args.profile).read_text(encoding='utf-8')) if args.profile else Profile()
    if calls:
        if not args.merge_state or not args.profile or not args.quota_db or not args.quota_limit:
            raise ValueError('live_requires_merge_state_profile_existing_quota_db_and_quota_limit')
        _existing(args.quota_db, 'quota_db')
        # Validate existing shared settings before SQLiteQuota may initialize tables.
        with sqlite3.connect(Path(args.quota_db).resolve().as_uri() + '?mode=ro', uri=True) as db:
            configured = db.execute('SELECT quota FROM settings WHERE id=1').fetchone()
            db.execute('SELECT count(*) FROM reservations').fetchone()
        if configured != (args.quota_limit,):
            raise ValueError('shared_quota_limit_mismatch')
        limiter, provider = SQLiteQuota(args.quota_db, args.quota_limit), OpenAIProvider()
    else:
        if args.quota_db or args.quota_limit:
            raise ValueError('quota_options_require_positive_max_calls')
        if args.merge_state:
            _existing(args.merge_state, 'merge_state')
        limiter = provider = _Offline()
    adapter = ClusterMergeAdapter(MergeService(provider, limiter, args.merge_state), profile) if args.merge_state else None
    return request, adapter


def _encoder(spec):
    if not spec:
        return None
    module, separator, factory = spec.partition(':')
    if not separator or not module or not factory or '.' in factory:
        raise ValueError('encoder_factory_expected_module_colon_function')
    return getattr(importlib.import_module(module), factory)()


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == 'import':
            bundle, diagnostics = _inputs(args)
            with SQLiteSearchStore(args.store) as store:
                result = store.import_bundle(**bundle)
            report = dict(command='import', result=result, diagnostics=diagnostics)
        elif args.command == 'validate':
            if args.articles or args.vectors or args.clusters:
                if args.store:
                    raise ValueError('validate_choose_inputs_or_store')
                bundle, diagnostics = _inputs(args)
                with SQLiteSearchStore(':memory:') as store:
                    store.import_bundle(**bundle)
                    report = dict(valid=True, **_summary(store.snapshot()), diagnostics=diagnostics)
            else:
                _existing(args.store, 'store')
                with SQLiteSearchStore(args.store) as store:
                    snapshot = store.snapshot()
                    from .models import CompanyInterestSet
                    empty = CompanyInterestSet(company_id='_', profile_version='_', interest_set_version='_',
                        encoder_fingerprint=snapshot.encoder_fingerprint, dimension=snapshot.dimension, interests=[])
                    exact_search(snapshot, empty)
                    report = dict(valid=True, **_summary(snapshot))
        elif args.command == 'inspect':
            _existing(args.store, 'store')
            with SQLiteSearchStore(args.store) as store:
                report = _summary(store.snapshot())
        else:
            _existing(args.store, 'store')
            if Path(args.output).exists():
                raise ValueError('output_already_exists_use_new_directory')
            request = RetrievalRequest.model_validate_json(Path(args.request).read_text(encoding='utf-8'))
            interests = read_interests(args.interests)
            request, merge = _merge(args, request)
            encoder = _encoder(args.encoder_factory)
            with SQLiteSearchStore(args.store) as store:
                run = RetrieverService().retrieve(request, interests=interests, store=store, merge=merge, encoder=encoder)
            output = Path(args.output)
            write_run(run, output)
            report = dict(status=run.batch.status, returned=len(run.batch.candidates), stop_reason=run.batch.stop_reason,
                          new_calls=run.trace['calls_new'], output=str(output))
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except ValidationError as exc:
        errors = ', '.join('.'.join(map(str, e['loc']))+':'+e['type'] for e in exc.errors()[:5])
        print('error: validation: '+errors, file=sys.stderr)
    except (ValueError, OSError, sqlite3.Error, ImportError, AttributeError, KeyError, TypeError) as exc:
        # Avoid dumping source rows, provider responses, or local factory internals.
        message = str(exc).splitlines()[0][:180] if str(exc) else type(exc).__name__
        print('error: '+message, file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
