from llm.events.paths import ROOT
"""Publish, inspect and export B6; all operations are offline."""
import argparse
import json
from pathlib import Path
import sqlite3

from llm.events.publication.service import EventBundleService
from llm.events.storage.migration import migrate_v1_to_v2, migrate_v2_to_v3, migrate_v3_to_v4
from llm.events.publication.models import PublicationConfig, FeatureDictionary, BundleProjection, PublicationManifest
from llm.events.storage.store import EventStore, DEFAULT_DB, StorageError
from llm.events.contracts.validation import ContractValidationError, _parse


def schemas():
    return {name + '.b6.1.schema.json': model.model_json_schema() for name, model in (
        ('publication-config', PublicationConfig), ('publication-features', FeatureDictionary),
        ('bundle-projection', BundleProjection), ('publication-manifest', PublicationManifest))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--catalog', default='default')
    commands = parser.add_subparsers(dest='command', required=True)
    schema = commands.add_parser('schema')
    schema.add_argument('--check', action='store_true')
    migrate = commands.add_parser('migrate')
    migrate.add_argument('--backup', type=Path)
    for name in ('plan', 'publish'):
        command = commands.add_parser(name)
        command.add_argument('--config', type=Path)
        if name == 'publish':
            command.add_argument('--rebuild', action='store_true')
    get = commands.add_parser('get')
    get.add_argument('event_id')
    selectors = get.add_mutually_exclusive_group()
    selectors.add_argument('--event-version', type=int)
    selectors.add_argument('--publication-revision', type=int)
    export = commands.add_parser('export')
    export.add_argument('--output', type=Path)
    export.add_argument('--publication-revision', type=int)
    export.add_argument('--active-only', action='store_true')
    changes = commands.add_parser('changes')
    changes.add_argument('--since', type=int, required=True)
    provenance = commands.add_parser('provenance')
    provenance.add_argument('event_id')
    provenance.add_argument('--event-version', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'schema':
            from jsonschema import Draft202012Validator
            for name, schema in schemas().items():
                schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
                Draft202012Validator.check_schema(schema)
                path = ROOT / 'docs/contracts' / name
                if args.check:
                    if not path.exists() or json.loads(path.read_text()) != schema:
                        raise StorageError('schema_drift', name)
                else:
                    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + '\n')
            result = {'status': 'checked' if args.check else 'generated'}
        elif not args.db.is_file():
            raise StorageError('database_not_found', 'Prepare the B5 database first')
        elif args.command == 'migrate':
            conn = sqlite3.connect(args.db)
            try:
                version = conn.execute('PRAGMA user_version').fetchone()[0]
            finally:
                conn.close()
            steps = []
            for source, fn in ((1, migrate_v1_to_v2), (2, migrate_v2_to_v3), (3, migrate_v3_to_v4)):
                if version == source:
                    steps.append(fn(args.db, args.backup if not steps else None))
                    version += 1
            if not steps:
                raise StorageError('unsupported_storage_schema', 'No supported migration step was selected')
            result = {'status': 'migrated', 'steps': steps}
        else:
            service = EventBundleService(EventStore(args.db), args.catalog)
            if args.command in ('plan', 'publish'):
                config = _parse(PublicationConfig, json.loads(args.config.read_text())) if args.config else None
                if args.command == 'plan':
                    result = service.plan_publication(config)
                else:
                    result = service.publish(config=config, rebuild=args.rebuild or config is not None)
            elif args.command == 'get':
                result = service.get_event(args.event_id, event_version=args.event_version,
                    publication_revision=args.publication_revision)
            elif args.command == 'changes':
                result = service.changes_since(args.since)
            elif args.command == 'provenance':
                result = service.get_provenance(args.event_id, args.event_version)
            else:
                result = service.export_snapshot(args.output, publication_revision=args.publication_revision,
                    include_withdrawn=not args.active_only)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ContractValidationError as exc:
        result = {'status': 'error', 'issues': [i.model_dump() for i in exc.issues]}
    except (OSError, ValueError, TypeError, sqlite3.Error):
        result = {'status': 'error', 'issues': [
            {'code': 'invalid_input', 'path': '/', 'message': 'Unable to read or parse input'}]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
