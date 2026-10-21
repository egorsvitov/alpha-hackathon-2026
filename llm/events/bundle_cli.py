"""Publish and deliver B6 EventBundle v0.2; no network or LLM calls."""
import argparse
import json
from pathlib import Path
import sqlite3

from .bundle_store import EventBundleService
from .migration import migrate_v1_to_v2, migrate_v2_to_v3, migrate_v3_to_v4
from .storage import EventStore, DEFAULT_DB, StorageError
from .validation import ContractValidationError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--catalog', default='default')
    commands = parser.add_subparsers(dest='command', required=True)
    migrate = commands.add_parser('migrate')
    migrate.add_argument('--backup', type=Path)
    publish = commands.add_parser('publish')
    publish.add_argument('--rebuild', action='store_true')
    get = commands.add_parser('get')
    get.add_argument('event_id')
    export = commands.add_parser('export')
    export.add_argument('--output', type=Path, required=True)
    export.add_argument('--active-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        if not args.db.is_file():
            raise StorageError('database_not_found', 'Prepare the B5 database first')
        if args.command == 'migrate':
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
            if args.command == 'publish':
                result = service.publish(rebuild=args.rebuild)
            elif args.command == 'get':
                result = service.get_event(args.event_id)
            else:
                result = service.export_events(args.output, include_withdrawn=not args.active_only)
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
