"""Explicit B2 -> B3 migration; no implicit mutation of old databases."""
from pathlib import Path
import sqlite3
from uuid import uuid4
from llm.events.storage.store import StorageError, EventStore
from llm.events.storage.schema import APPLICATION_ID, B2_TABLES, B3_DDL, B5_DDL, V2_TABLES, B6_DDL, V3_TABLES


def _migrate(db_path, backup_path, source_version, target_version, required_tables, statements, forbidden_tables):
    path=Path(db_path)
    if not path.is_file():
        raise StorageError('database_not_found','Migration requires an existing B2 database')
    backup=Path(backup_path) if backup_path else path.with_name(path.name+f'.v{source_version}-{uuid4().hex}.backup')
    if backup.resolve()==path.resolve():
        raise StorageError('invalid_backup_path','Backup must differ from the database')
    conn=sqlite3.connect(path, isolation_level=None,timeout=5)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        app=conn.execute('PRAGMA application_id').fetchone()[0]
        version=conn.execute('PRAGMA user_version').fetchone()[0]
        tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if app!=APPLICATION_ID or version!=source_version or not required_tables.issubset(tables) or tables & forbidden_tables:
            raise StorageError('unsupported_storage_schema',f'Only a complete schema v{source_version} can be migrated')
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise StorageError('storage_integrity_error','Foreign key violations in the existing database')
        backup.parent.mkdir(parents=True,exist_ok=True)
        # Reserved writer lock prevents concurrent mutations. A separate reader
        # backs up the committed v1 snapshot, including committed WAL pages.
        with backup.open('xb'):
            pass
        backup.chmod(0o600)
        reader=sqlite3.connect(path)
        target=sqlite3.connect(backup)
        try:
            reader.backup(target)
        finally:
            target.close(); reader.close()
        for statement in statements:
            conn.execute(statement)
        conn.execute(f'PRAGMA user_version={target_version}')
        conn.commit()
        return {'schema_version':target_version,'backup_path':str(backup)}
    except BaseException as exc:
        conn.rollback()
        if isinstance(exc,sqlite3.Error):
            raise EventStore._sqlite_error(exc) from exc
        if isinstance(exc,OSError):
            raise StorageError('storage_io_error',str(exc)) from exc
        raise
    finally:
        conn.close()


def migrate_v1_to_v2(db_path, backup_path=None):
    return _migrate(db_path,backup_path,1,2,B2_TABLES,B3_DDL,{'budget_days','llm_calls'})


def migrate_v2_to_v3(db_path, backup_path=None):
    return _migrate(db_path,backup_path,2,3,V2_TABLES,B5_DDL,{'catalogs','catalog_runs'})


def migrate_v3_to_v4(db_path, backup_path=None):
    return _migrate(db_path, backup_path, 3, 4, V3_TABLES, B6_DDL,
                    {'bundle_publications', 'bundle_versions', 'bundle_publication_events'})
