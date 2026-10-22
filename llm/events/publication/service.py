"""Explicit, atomic publication and read-only delivery of EventBundle v0.2."""
from datetime import datetime, timezone
import hashlib
import json

from uuid import uuid4

from llm.events.publication.builder import build_bundle, RULES_VERSION
from llm.events.catalog.models import CanonicalEventRecord, ClaimContext, PairDecision
from llm.events.storage.store import StorageError, canonical_json, _text
from llm.events.contracts.validation import validate_bundle, validate_grounding, _parse
from llm.events.contracts.models import EventBundle
from llm.events.publication.features import resolved_rules, publication_configuration_hash, derive_features, assess_quality, validate_feature_trace
from llm.events.publication.models import PublicationConfig, PublicationContext, NormalizedInput, BundleProjection
from llm.events.catalog.service import CatalogService


def rules_configuration(config=None):
    config = config or PublicationConfig()
    return {'rules_version': RULES_VERSION,
            'configuration': config.model_dump(mode='json'),
            'dictionary': resolved_rules(config).model_dump(mode='json'),
            'implementation_hash': publication_configuration_hash(config),
            'bundle_schema_sha256': _hash(EventBundle.model_json_schema())}


def _rules(publication):
    if not publication:
        return None
    value = json.loads(publication['rules_json'])
    return value.get('rules', value)  # Also read publications written by the first B6 producer.


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
            normalized_inputs = []
            for aid in sorted({entry['artifact_id'] for entry in manifest['inputs']
                    if entry['source_id'] == manifest['heads'].get(entry['article_id'])}):
                result = CatalogService(self.store, self.catalog_id)._load_artifact(aid)
                normalized_inputs.append(NormalizedInput(artifact_id=aid, result=result))
                qualities[aid] = result.quality
            articles = []
            for sid in sorted({s.source_id for r in records for s in r.sources}):
                row = self.store._require(conn.execute(
                    'SELECT * FROM article_versions WHERE source_id=?', (sid,)).fetchone(), 'article', sid)
                articles.append(self.store._record(row))
            publication = self._latest(conn)
            previous = self._bundles(conn, publication)
            return {'catalog': catalog, 'run_id': run['run_id'], 'records': records,
                    'contexts': contexts, 'qualities': qualities, 'articles': articles,
                    'normalized_inputs': normalized_inputs,
                    'missing': manifest['missing_articles'],
                    'decisions': [PairDecision.model_validate(d) for d in json.loads(run['decisions_json'])],
                    'publication': dict(publication) if publication else None, 'previous': previous}

    def _prepare(self, config=None, *, rebuild=False):
        before = self._capture()
        last = before['publication']
        if config is None and not rebuild and last and _rules(last).get('configuration'):
            config = PublicationConfig.model_validate(_rules(last)['configuration'])
            # A default dictionary must also stay pinned across rule-file changes.
            if config.dictionary is None and _rules(last).get('dictionary'):
                config = PublicationConfig.model_validate({**config.model_dump(),
                    'dictionary': _rules(last)['dictionary']})
        else:
            config = _parse(PublicationConfig, config or PublicationConfig())
        config = config.model_copy(update={'dictionary': resolved_rules(config)})
        rules = rules_configuration(config)
        if last and _rules(last) != rules and not rebuild:
            raise StorageError('bundle_configuration_mismatch', 'B6 rules changed; use publish --rebuild')
        if len(before['records']) > config.max_events:
            raise StorageError('publication_limit_exceeded', 'Event count exceeds max_events')
        now = datetime.fromtimestamp(self.store.clock(), timezone.utc).isoformat()
        context = PublicationContext(articles=before['articles'],
            normalized_inputs=before['normalized_inputs'], decisions=before['decisions'],
            missing_articles=before['missing'],
            catalog_claim_ids=sorted(c.claim_id for r in before['records'] for c in r.claims))
        prepared, provenance = {}, {}
        contexts = {c.claim.claim_id: c for c in before['contexts']}
        for record in before['records']:
            if any(c.claim_id not in contexts for c in record.claims):
                raise StorageError('catalog_integrity_error', 'A current claim lacks its B5 context')
            bundle = build_bundle(record, context, config, published_at=now)
            payload = bundle.model_dump(mode='json')
            previous = before['previous'].get(record.event_id)
            if previous:
                if _content(previous) == _content(payload):
                    payload = previous
                else:
                    payload['event']['event_version'] = previous['event']['event_version'] + 1
            validate_bundle(payload)
            validate_grounding(payload, before['articles'])
            _, traces = derive_features(record, resolved_rules(config))
            _, reasons = assess_quality(record, context)
            projection = BundleProjection(bundle=payload, feature_trace=traces, quality_reasons=reasons)
            validate_feature_trace(projection)
            prepared[record.event_id] = payload
            claim_ids = {c.claim_id for c in record.claims}
            provenance[record.event_id] = {
                'catalog_revision': before['catalog']['revision'],
                'catalog_generation': before['catalog']['generation'],
                'catalog_event_version': record.event_version, 'catalog_run_id': before['run_id'],
                'artifact_ids': sorted({c.artifact_id for c in before['contexts'] if c.claim.claim_id in claim_ids}),
                'decision_ids': record.decision_ids, 'replaced_by': record.replaced_by,
                'feature_trace': [t.model_dump(mode='json') for t in traces],
                'quality_reasons': [r.model_dump(mode='json') for r in reasons],
                'semantic_support': 'not_assessed'}
        return before, config, rules, prepared, provenance, now

    def plan_publication(self, config=None):
        before, _, rules, prepared, provenance, _ = self._prepare(config, rebuild=config is not None)
        changed = [eid for eid, value in prepared.items() if value != before['previous'].get(eid)]
        return {'status': 'planned', 'catalog_revision': before['catalog']['revision'],
                'changed_event_ids': changed, 'rules': rules, 'events': prepared,
                'provenance': provenance}

    def publish_snapshot(self, config=None):
        return self.publish(config=config, rebuild=config is not None)

    def publish(self, *, rebuild=False, config=None):
        if type(rebuild) is not bool:
            raise StorageError('invalid_argument', 'rebuild must be a boolean')
        before, config, rules, prepared, provenance, now = self._prepare(config, rebuild=rebuild)
        last = before['publication']
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
            if rules_configuration(config) != rules:
                raise StorageError('bundle_configuration_mismatch', 'B6 rules changed during preparation')
            if last and last['run_id'] == before['run_id'] and _rules(last) == rules:
                return {'status': 'cached', 'publication_id': last['publication_id'],
                        'catalog_id': self.catalog_id, 'revision': current['revision'],
                        'publication_revision': last['sequence'], 'changed_event_ids': []}
            pid = str(uuid4())
            conn.execute('INSERT INTO bundle_publications VALUES (?,?,?,?,?,?,?)',
                (pid, self.catalog_id, last['sequence'] + 1 if last else 1, before['run_id'],
                 current['generation'], canonical_json({'rules': rules, 'provenance': provenance,
                    'selected_artifact_ids': sorted(i.artifact_id for i in before['normalized_inputs'])}), now))
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
                'revision': before['catalog']['revision'],
                'publication_revision': last['sequence'] + 1 if last else 1, 'changed_event_ids': changed}

    def _read(self, event_id=None, publication_revision=None):
        if publication_revision is not None and (type(publication_revision) is not int or publication_revision < 1):
            raise StorageError('invalid_argument', 'publication_revision must be a positive integer')
        with self.store._transaction() as conn:
            if publication_revision is not None:
                publication = conn.execute('SELECT * FROM bundle_publications WHERE catalog_id=? AND sequence=?',
                    (self.catalog_id, publication_revision)).fetchone()
                if publication is None:
                    raise StorageError('publication_not_found', str(publication_revision))
            else:
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

    def get_event(self, event_id, *, event_version=None, publication_revision=None):
        _text(event_id, 'event_id')
        if event_version is not None and publication_revision is not None:
            raise StorageError('invalid_argument', 'Historical selectors are mutually exclusive')
        if event_version is not None:
            if type(event_version) is not int or event_version < 1:
                raise StorageError('invalid_argument', 'event_version must be a positive integer')
            with self.store._transaction() as conn:
                row = conn.execute('SELECT v.* FROM bundle_versions v JOIN catalog_events c USING(event_id) '
                    'WHERE c.catalog_id=? AND v.event_id=? AND v.event_version=?',
                    (self.catalog_id, event_id, event_version)).fetchone()
                if row is None:
                    raise StorageError('event_not_found', event_id)
                payload = json.loads(row['payload_json'])
                if _hash(payload) != row['payload_hash']:
                    raise StorageError('bundle_integrity_error', 'Stored bundle hash does not match')
                validate_bundle(payload)
                return payload
        _, bundles = self._read(event_id=event_id, publication_revision=publication_revision)
        if event_id not in bundles:
            raise StorageError('event_not_found', event_id)
        return bundles[event_id]

    def changes_since(self, publication_revision):
        old, old_bundles = self._read(publication_revision=publication_revision)
        current, bundles = self._read()
        if old['sequence'] > current['sequence']:
            raise StorageError('invalid_argument', 'Baseline is newer than the current publication')
        return {'publication_revision': current['sequence'], 'since': old['sequence'],
            'events': [b for eid, b in sorted(bundles.items()) if b != old_bundles.get(eid)]}

    def get_provenance(self, event_id, event_version):
        self.get_event(event_id, event_version=event_version)
        with self.store._transaction() as conn:
            row = conn.execute('SELECT p.* FROM bundle_publications p JOIN bundle_publication_events e '
                'USING(publication_id) WHERE p.catalog_id=? AND e.event_id=? AND e.event_version=? '
                'ORDER BY p.sequence LIMIT 1', (self.catalog_id, event_id, event_version)).fetchone()
        stored = json.loads(row['rules_json'])
        result = stored.get('provenance', {}).get(event_id)
        if result is None:
            raise StorageError('provenance_unavailable', 'This publication predates feature provenance; republish explicitly')
        return {**result, 'rules': _rules(row), 'publication_revision': row['sequence'],
                'event_id': event_id, 'event_version': event_version}

    def export_events(self, output_path=None, *, publication_revision=None, include_withdrawn=True):
        from llm.events.publication.export import export_events
        if type(include_withdrawn) is not bool:
            raise StorageError('invalid_argument', 'include_withdrawn must be a boolean')
        if output_path is None:
            return [b for b in self._read(publication_revision=publication_revision)[1].values()
                    if include_withdrawn or b['event']['availability'] != 'withdrawn']
        return export_events(self, output_path, publication_revision=publication_revision,
                             include_withdrawn=include_withdrawn)

    def export_snapshot(self, output_path=None, *, publication_revision=None, include_withdrawn=True):
        from llm.events.publication.export import export_events
        return export_events(self, output_path, publication_revision=publication_revision,
                             include_withdrawn=include_withdrawn)
