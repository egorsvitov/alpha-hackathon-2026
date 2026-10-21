"""Explicit, atomic publication and read-only delivery of EventBundle v0.2."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from .bundle import build_bundle, RULES_VERSION
from .catalog_models import CanonicalEventRecord, ClaimContext, PairDecision
from .catalog_validation import validate_catalog_record
from .normalization_models import NormalizedArticleResult
from .storage import StorageError, canonical_json, _text
from .validation import validate_bundle, validate_grounding


def rules_configuration():
    # Include the executable identity rules and validators used by the projection.
    names = ('bundle.py', 'bundle_store.py', 'matching.py', 'catalog_models.py',
             'models.py', 'validation.py', 'catalog_validation.py', 'normalization_models.py')
    hashes = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
              for name in names}
    return {'rules_version': RULES_VERSION,
            'implementation_hash': hashlib.sha256(canonical_json(hashes).encode()).hexdigest()}


def _hash(payload):
    return hashlib.sha256(canonical_json(payload).encode('utf-8')).hexdigest()


def _content(payload):
    value = {**payload, 'event': dict(payload['event'])}
    value['event'].pop('event_version')
    value['event'].pop('updated_at')
    return value


class EventBundleService:
    def __init__(self, store, catalog_id='default'):
        self.store = store
        self.catalog_id = _text(catalog_id, 'catalog_id')

    def _catalog(self, conn):
        row = self.store._require(conn.execute(
            'SELECT * FROM catalogs WHERE catalog_id=?', (self.catalog_id,)).fetchone(),
            'catalog', self.catalog_id)
        if row['stale']:
            raise StorageError('catalog_stale', 'Rebuild B5 before publishing or reading B6')
        return row

    def _latest(self, conn):
        return conn.execute('SELECT * FROM bundle_publications WHERE catalog_id=? '
                            'ORDER BY sequence DESC LIMIT 1', (self.catalog_id,)).fetchone()

    @staticmethod
    def _bundles(conn, publication, event_id=None):
        if publication is None:
            return {}
        result = {}
        query = ('SELECT v.* FROM bundle_publication_events e '
                 'JOIN bundle_versions v USING(event_id,event_version) '
                 'WHERE e.publication_id=?')
        args = [publication['publication_id']]
        if event_id is not None:
            query += ' AND e.event_id=?'
            args.append(event_id)
        for row in conn.execute(query + ' ORDER BY e.event_id', args):
            payload = json.loads(row['payload_json'])
            if _hash(payload) != row['payload_hash']:
                raise StorageError('bundle_integrity_error', 'Stored bundle hash does not match')
            validate_bundle(payload)
            result[row['event_id']] = payload
        return result

    def _capture(self):
        with self.store._transaction() as conn:
            catalog = dict(self._catalog(conn))
            run = self.store._require(conn.execute(
                'SELECT * FROM catalog_runs WHERE catalog_id=? AND revision=?',
                (self.catalog_id, catalog['revision'])).fetchone(),
                'catalog_revision', str(catalog['revision']))
            records = [CanonicalEventRecord.model_validate(json.loads(r['payload_json']))
                for r in conn.execute('SELECT v.payload_json FROM catalog_run_events e '
                    'JOIN catalog_event_versions v USING(event_id,event_version) '
                    'WHERE e.run_id=? ORDER BY e.event_id', (run['run_id'],))]
            manifest = json.loads(run['manifest_json'])
            contexts = [ClaimContext.model_validate(c) for c in manifest['contexts']]
            qualities = {}
            for aid in sorted({c.artifact_id for c in contexts}):
                artifact = self.store._require(conn.execute(
                    'SELECT payload_json FROM artifacts WHERE artifact_id=?', (aid,)).fetchone(),
                    'artifact', aid)
                qualities[aid] = NormalizedArticleResult.model_validate(
                    json.loads(artifact['payload_json'])).quality
            articles = []
            for sid in sorted({s.source_id for r in records for s in r.sources}):
                row = self.store._require(conn.execute(
                    'SELECT * FROM article_versions WHERE source_id=?', (sid,)).fetchone(), 'article', sid)
                articles.append(self.store._record(row))
            publication = self._latest(conn)
            previous = self._bundles(conn, publication)
            return {'catalog': catalog, 'run_id': run['run_id'], 'records': records,
                    'contexts': contexts, 'qualities': qualities, 'articles': articles,
                    'missing': manifest['missing_articles'],
                    'decisions': [PairDecision.model_validate(d) for d in json.loads(run['decisions_json'])],
                    'publication': dict(publication) if publication else None, 'previous': previous}

    def publish(self, *, rebuild=False):
        if type(rebuild) is not bool:
            raise StorageError('invalid_argument', 'rebuild must be a boolean')
        before = self._capture()
        rules = rules_configuration()
        last = before['publication']
        if last and json.loads(last['rules_json']) != rules and not rebuild:
            raise StorageError('bundle_configuration_mismatch', 'B6 rules changed; use publish --rebuild')
        now = datetime.fromtimestamp(self.store.clock(), timezone.utc).isoformat()
        owners = {c.claim_id: r.event_id for r in before['records'] for c in r.claims}
        prepared = {}
        contexts = {c.claim.claim_id: c for c in before['contexts']}
        for record in before['records']:
            validate_catalog_record(record, before['articles'])
            if any(c.claim_id not in contexts for c in record.claims):
                raise StorageError('catalog_integrity_error', 'A current claim lacks its B5 context')
            bundle = build_bundle(record, contexts=[contexts[c.claim_id] for c in record.claims],
                b4_qualities=before['qualities'],
                decisions=before['decisions'], claim_events=owners, missing_articles=before['missing'],
                updated_at=now)
            payload = bundle.model_dump(mode='json')
            previous = before['previous'].get(record.event_id)
            if previous:
                if _content(previous) == _content(payload):
                    payload = previous
                else:
                    payload['event']['event_version'] = previous['event']['event_version'] + 1
            validate_bundle(payload)
            validate_grounding(payload, before['articles'])
            prepared[record.event_id] = payload
        with self.store._transaction(write=True) as conn:
            # Check stale here as a concurrent-change error, not as a read failure.
            current = conn.execute('SELECT * FROM catalogs WHERE catalog_id=?', (self.catalog_id,)).fetchone()
            latest = self._latest(conn)
            if (current is None or current['stale'] or
                    (current['revision'], current['generation']) !=
                    (before['catalog']['revision'], before['catalog']['generation']) or
                    (latest['publication_id'] if latest else None) !=
                    (last['publication_id'] if last else None)):
                raise StorageError('catalog_changed', 'Catalog or B6 publication changed during preparation')
            if rules_configuration() != rules:
                raise StorageError('bundle_configuration_mismatch', 'B6 rules changed during preparation')
            if last and last['run_id'] == before['run_id'] and json.loads(last['rules_json']) == rules:
                return {'status': 'cached', 'publication_id': last['publication_id'],
                        'catalog_id': self.catalog_id, 'revision': current['revision'], 'changed_event_ids': []}
            pid = str(uuid4())
            conn.execute('INSERT INTO bundle_publications VALUES (?,?,?,?,?,?,?)',
                (pid, self.catalog_id, last['sequence'] + 1 if last else 1, before['run_id'],
                 current['generation'], canonical_json(rules), now))
            changed = []
            for record in before['records']:
                payload = prepared[record.event_id]
                version = payload['event']['event_version']
                previous = before['previous'].get(record.event_id)
                if previous is None or previous['event']['event_version'] != version:
                    conn.execute('INSERT INTO bundle_versions VALUES (?,?,?,?)',
                        (record.event_id, version, canonical_json(payload), _hash(payload)))
                    changed.append(record.event_id)
                conn.execute('INSERT INTO bundle_publication_events VALUES (?,?,?,?)',
                    (pid, record.event_id, version, record.event_version))
        return {'status': 'succeeded', 'publication_id': pid, 'catalog_id': self.catalog_id,
                'revision': before['catalog']['revision'], 'changed_event_ids': changed}

    def _read(self, event_id=None):
        with self.store._transaction() as conn:
            catalog = self._catalog(conn)
            publication = self._latest(conn)
            if publication is None:
                raise StorageError('bundle_not_published', 'Run B6 publish first')
            run = conn.execute('SELECT revision FROM catalog_runs WHERE run_id=?',
                               (publication['run_id'],)).fetchone()
            if (run['revision'] != catalog['revision'] or
                    publication['catalog_generation'] != catalog['generation']):
                raise StorageError('b6_stale', 'Publish B6 after updating B5')
            return dict(publication), self._bundles(conn, publication, event_id=event_id)

    def get_event(self, event_id):
        _text(event_id, 'event_id')
        _, bundles = self._read(event_id=event_id)
        if event_id not in bundles:
            raise StorageError('event_not_found', event_id)
        return bundles[event_id]

    def export_events(self, output_path, *, include_withdrawn=True):
        if type(include_withdrawn) is not bool:
            raise StorageError('invalid_argument', 'include_withdrawn must be a boolean')
        publication, bundles = self._read()
        path = Path(output_path)
        # Prevent accidentally replacing the NLP database or one of its WAL files.
        if path.resolve() in {Path(str(self.store.path) + suffix).resolve() for suffix in ('', '-wal', '-shm')}:
            raise StorageError('invalid_argument', 'Output must differ from the SQLite database')
        temporary = None
        count = 0
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                    dir=path.parent, prefix='.' + path.name + '.', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                for eid in sorted(bundles):
                    payload = bundles[eid]
                    if not include_withdrawn and payload['event']['availability'] == 'withdrawn':
                        continue
                    stream.write(canonical_json(payload) + '\n')
                    count += 1
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise StorageError('storage_io_error', str(exc)) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {'status': 'exported', 'publication_id': publication['publication_id'],
                'count': count, 'output_path': str(path)}
