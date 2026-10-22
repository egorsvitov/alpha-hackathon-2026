"""Export a single publication; prepare files before taking the final SQLite fence."""
import hashlib

import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

from llm.events.contracts.models import EventBundle
from llm.events.publication.models import PublicationManifest
from llm.events.storage.store import StorageError, canonical_json


def _write(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def export_events(service, output_path=None, *, publication_revision=None, include_withdrawn=True):
    if type(include_withdrawn) is not bool:
        raise StorageError('invalid_argument', 'include_withdrawn must be a boolean')
    publication, bundles = service._read(publication_revision=publication_revision)
    path = Path(output_path) if output_path is not None else (
        service.store.path.parent / 'exports' /
        f"publication-{publication['sequence']}-{uuid4().hex}")
    # Explicit .jsonl paths retain the old single-file API. Directory exports
    # provide the full manifest contract and are used by the CLI by default.
    legacy = path.suffix == '.jsonl'
    protected = {Path(str(service.store.path) + suffix).resolve() for suffix in ('', '-wal', '-shm')}
    if path.resolve() in protected:
        raise StorageError('invalid_argument', 'Output must differ from the SQLite database')
    if path.exists() and not legacy:
        raise StorageError('export_exists', 'Choose a new export directory; completed exports are immutable')
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp'))
        selected = [b for _, b in sorted(bundles.items())
                    if include_withdrawn or b['event']['availability'] != 'withdrawn']
        data = ''.join(canonical_json(b) + '\n' for b in selected).encode('utf-8')
        _write(temporary / 'events.jsonl', data)
        from llm.events.publication.service import _rules, _hash
        with service.store._transaction() as conn:
            run = conn.execute('SELECT revision FROM catalog_runs WHERE run_id=?',
                (publication['run_id'],)).fetchone()
        rules = _rules(publication)
        manifest = PublicationManifest(catalog_id=service.catalog_id,
            publication_revision=publication['sequence'], catalog_revision=run['revision'],
            catalog_run_id=publication['run_id'], published_at=publication['created_at'],
            configuration_sha256=_hash(rules),
            bundle_schema_sha256=rules.get('bundle_schema_sha256') or _hash(EventBundle.model_json_schema()),
            count=len(selected), complete_snapshot=include_withdrawn,
            events_sha256=hashlib.sha256(data).hexdigest()).model_dump(mode='json')
        _write(temporary / 'manifest.json', (canonical_json(manifest) + '\n').encode('utf-8'))
        # BEGIN IMMEDIATE prevents imports, B5 rebuilds and B6 publishers on all
        # connections until the prepared export is made visible. No file content
        # is written while holding this short writer reservation.
        with service.store._transaction(write=True) as conn:
            if publication_revision is None:
                catalog = service._catalog(conn)
                current = service._latest(conn)
                if (catalog['generation'] != publication['catalog_generation'] or
                        catalog['revision'] != run['revision']):
                    raise StorageError('catalog_changed', 'Catalog changed while preparing export')
                if current['publication_id'] != publication['publication_id']:
                    raise StorageError('publication_changed', 'Publication changed while preparing export')
            os.replace(temporary / 'events.jsonl' if legacy else temporary, path)
        return {'status': 'exported', 'publication_id': publication['publication_id'],
                'publication_revision': publication['sequence'], 'count': len(selected),
                'output_path': str(path), 'complete_snapshot': include_withdrawn,
                'format': 'legacy_jsonl' if legacy else 'directory',
                'manifest': manifest if not legacy else None}
    except OSError as exc:
        raise StorageError('storage_io_error', str(exc)) from exc
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)
