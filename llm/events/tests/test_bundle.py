"""Offline B6 projection, publication, migrations and consumer boundary."""
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker
from llm.events import EventBundleService, EventStore, StorageError
from llm.events.bundle import build_bundle
from llm.events.bundle_store import rules_configuration
from llm.events.catalog_models import CanonicalEventRecord, ClaimContext, ReviewRequest
from llm.events.migration import migrate_v3_to_v4
from llm.events.models import ArticleRecord
from llm.events.storage_schema import V3_DDL, V3_TABLES
from llm.events.validation import validate_bundle, validate_grounding, ContractValidationError
from llm.events.tests import test_catalog as catalog_tests

ROOT = Path(__file__).resolve().parents[3]


class BundleTests(unittest.TestCase):
    def setUp(self):
        # Reuse the established synthetic B2 -> scripted B3 -> B4 fixture workflow.
        self.helper = catalog_tests.CatalogTests(methodName='runTest')
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.store = self.helper.store
        self.catalog = self.helper.service
        self.service = EventBundleService(self.store)
        self.output = Path(self.helper.temp.name) / 'events.jsonl'

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(StorageError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def ready(self, *values):
        artifacts = [self.helper.publish(chr(97 + i), value) for i, value in enumerate(values or ('14',))]
        self.helper.initialize(*artifacts)
        return self.catalog.list_events()[0].event_id

    def count(self, table):
        with self.store._transaction() as conn:
            return conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]

    def test_all_seven_examples_project_without_losing_source_semantics(self):
        articles = [ArticleRecord.model_validate(a) for a in json.loads(
            (ROOT / 'llm/events/fixtures/b6_articles.json').read_text())]
        schema = json.loads((ROOT / 'docs/contracts/event-bundle.v0.2.schema.json').read_text())
        cases = list((ROOT / 'docs/b6-examples/v0.2').glob('*.json'))
        self.assertEqual(len(cases), 7)
        for path in cases:
            with self.subTest(path=path.name):
                original = validate_bundle(json.loads(path.read_text()))
                sources = {s.source_id: s for s in original.sources}
                evidence = {e.evidence_id: e for e in original.evidence}
                contexts = []
                for claim in original.claims:
                    spans = [evidence[eid] for eid in claim.evidence_ids]
                    contexts.append(ClaimContext(artifact_id='offline', candidate_id=claim.claim_id,
                        event_type=original.event.event_type, claim=claim,
                        source=sources[spans[0].source_id], evidence=spans))
                record = CanonicalEventRecord(catalog_id='default', event_id=original.event.event_id,
                    event_version=8, event_type=original.event.event_type, availability='active',
                    updated_at=original.event.updated_at, anchor_claim_id=original.claims[0].claim_id,
                    claims=original.claims, sources=original.sources, evidence=original.evidence,
                    conflicts=original.conflicts, relations=original.relations,
                    limitations=original.limitations, decision_ids=[])
                bundle = build_bundle(record, contexts=contexts, b4_qualities={'offline': 'complete'},
                    updated_at=record.updated_at)
                validate_grounding(bundle, articles)
                payload = bundle.model_dump(mode='json')
                Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
                for group in ('claims', 'sources', 'evidence', 'conflicts', 'relations'):
                    self.assertEqual(payload[group], original.model_dump(mode='json')[group])
                self.assertEqual(bundle.event.event_version, 1)
                self.assertEqual(bundle.ranking_features.topics, ['rates'] if record.event_type == 'policy_rate' else ['industry'])

    def test_end_to_end_api_export_cache_and_read_only(self):
        eid = self.ready('14', '15')
        self.error('bundle_not_published', self.service.get_event, eid)
        receipt = self.service.publish()
        self.assertEqual(receipt['status'], 'succeeded')
        bundle = self.service.get_event(eid)
        self.assertEqual(bundle['schema_version'], '0.2')
        self.assertEqual(bundle['event']['quality'], 'complete')
        self.assertTrue(bundle['conflicts'])
        self.assertNotIn('14', bundle['event']['title'])
        self.assertNotIn('15', bundle['event']['title'])
        self.service.export_events(self.output)
        self.assertEqual([json.loads(line) for line in self.output.read_text().splitlines()], [bundle])
        self.assertEqual(self.service.publish()['status'], 'cached')
        self.assertEqual(self.count('bundle_versions'), 1)
        # Even an updated wall clock cannot change a read or a cached publication.
        self.helper.clock.value += 100
        with patch.object(self.store, '_transaction', wraps=self.store._transaction) as transactions:
            self.assertEqual(self.service.get_event(eid), bundle)
            self.service.export_events(self.output)
            self.assertTrue(all(not c.kwargs.get('write', False) for c in transactions.call_args_list))
        self.assertEqual(self.service.publish()['status'], 'cached')
        self.assertEqual(self.service.get_event(eid), bundle)
        self.error('event_not_found', self.service.get_event, 'unknown')
        # Caller mutation does not change the stored version.
        bundle['claims'].clear()
        self.assertTrue(self.service.get_event(eid)['claims'])
        self.error('invalid_argument', self.service.export_events, self.store.path)

    def test_stale_withdrawn_and_active_only(self):
        eid = self.ready()
        self.service.publish()
        old = self.service.get_event(eid)
        raw = self.store.get_current_article('a').article.model_dump(mode='json')
        raw['title'] += ' исправлено'
        self.store.import_batch(json.dumps([raw]).encode())
        for fn, args in ((self.service.get_event, (eid,)), (self.service.export_events, (self.output,)),
                         (self.service.publish, ())):
            self.error('catalog_stale', fn, *args)
        self.catalog.rebuild_catalog()
        self.error('b6_stale', self.service.get_event, eid)
        self.service.publish()
        withdrawn = self.service.get_event(eid)
        self.assertEqual(withdrawn['event']['availability'], 'withdrawn')
        self.assertEqual(withdrawn['event']['quality'], 'insufficient')
        self.assertEqual(withdrawn['event']['event_version'], 2)
        for key in ('claims', 'sources', 'evidence', 'conflicts', 'relations'):
            self.assertEqual(withdrawn[key], [])
        self.service.export_events(self.output)
        self.assertEqual(json.loads(self.output.read_text()), withdrawn)
        self.service.export_events(self.output, include_withdrawn=False)
        self.assertEqual(self.output.read_bytes(), b'')
        with self.store._transaction() as conn:
            historical = json.loads(conn.execute('SELECT payload_json FROM bundle_versions '
                'WHERE event_id=? AND event_version=1', (eid,)).fetchone()[0])
        self.assertEqual(historical, old)

    def test_b5_version_changes_without_b6_version_change(self):
        eid = self.ready('14', '14')
        self.service.publish()
        old = self.service.get_event(eid)
        record = self.catalog.get_event(eid)
        # A manual reaffirmation changes the audit/decision IDs in B5 only.
        ids = sorted(c.claim_id for c in record.claims)
        self.catalog.review_pair(ReviewRequest(left_id=ids[0], right_id=ids[1],
            identity='same_event', reason='Same dated release independently reviewed'))
        self.assertGreater(self.catalog.get_event(eid).event_version, record.event_version)
        self.error('b6_stale', self.service.get_event, eid)
        self.assertEqual(self.service.publish()['changed_event_ids'], [])
        self.assertEqual(self.service.get_event(eid), old)
        self.assertEqual(self.count('bundle_versions'), 1)
        self.assertEqual(self.count('bundle_publications'), 2)
        with self.store._transaction() as conn:
            self.assertEqual(conn.execute('SELECT MAX(catalog_event_version) FROM '
                'bundle_publication_events').fetchone()[0], record.event_version + 1)

    def test_explicit_rules_rebuild_and_output_version(self):
        eid = self.ready()
        self.service.publish()
        old = self.service.get_event(eid)
        rules = {**rules_configuration(), 'rules_version': 'test-new'}
        with patch('llm.events.bundle_store.rules_configuration', return_value=rules):
            self.error('bundle_configuration_mismatch', self.service.publish)
            self.assertEqual(self.service.publish(rebuild=True)['changed_event_ids'], [])
            self.assertEqual(self.service.get_event(eid), old)
        rules['rules_version'] = 'test-newer'
        with patch('llm.events.bundle_store.rules_configuration', return_value=rules), \
                patch.dict('llm.events.bundle.TYPE_LABELS', {'policy_rate': 'Решение о ставке'}):
            self.service.publish(rebuild=True)
            self.assertEqual(self.service.get_event(eid)['event']['event_version'], 2)
        self.assertEqual(self.catalog.get_event(eid).event_version, 1)
        self.assertEqual(self.count('bundle_versions'), 2)

    def test_merge_withdraws_old_id_without_redirect(self):
        original = self.ready('14', '14')
        ids = sorted(c.claim_id for c in self.catalog.get_event(original).claims)
        split = self.catalog.review_pair(ReviewRequest(left_id=ids[0], right_id=ids[1],
            identity='different_event', reason='Distinct releases'))
        self.service.publish()
        other = next(r.event_id for r in self.catalog.list_events() if r.event_id != original)
        self.catalog.revoke_review(split['review_id'], 'Same release confirmed')
        self.service.publish()
        withdrawn = self.service.get_event(other)
        self.assertEqual(withdrawn['event']['event_id'], other)
        self.assertEqual(withdrawn['event']['availability'], 'withdrawn')
        self.assertIn('Replaced by event IDs: ' + original, withdrawn['limitations'])
        self.assertEqual(self.service.get_event(original)['event']['availability'], 'active')

    def test_empty_catalog_and_unknown_id(self):
        self.helper.initialize()
        self.service.publish()
        self.assertEqual(self.service.export_events(self.output)['count'], 0)
        self.assertEqual(self.output.read_bytes(), b'')
        self.error('event_not_found', self.service.get_event, 'missing')

    def test_invalid_grounding_rolls_back_whole_publication(self):
        eid = self.ready()
        real_build = build_bundle
        def broken(*args, **kwargs):
            bundle = real_build(*args, **kwargs)
            bundle.evidence[0].quote = 'x' * len(bundle.evidence[0].quote)
            return bundle
        with patch('llm.events.bundle_store.build_bundle', side_effect=broken):
            with self.assertRaises(ContractValidationError):
                self.service.publish()
        self.assertEqual(self.count('bundle_versions'), 0)
        self.assertEqual(self.count('bundle_publications'), 0)
        self.error('bundle_not_published', self.service.get_event, eid)

    def test_concurrent_catalog_and_publication_changes(self):
        self.ready()
        real_build = build_bundle
        with patch('llm.events.bundle_store.build_bundle', side_effect=lambda *a, **kw: real_build(*a, **kw)):
            # Publish through the capture boundary to avoid recursively mocking the builder.
            capture = self.service._capture
            def concurrent_capture():
                before = capture()
                with patch.object(self.service, '_capture', side_effect=capture):
                    self.service.publish()
                return before
            with patch.object(self.service, '_capture', side_effect=concurrent_capture):
                self.error('catalog_changed', self.service.publish)
        self.assertEqual(self.count('bundle_publications'), 1)
        def changed(*args, **kwargs):
            raw = self.store.get_current_article('a').article.model_dump(mode='json')
            raw['title'] += ' late'
            self.store.import_batch(json.dumps([raw]).encode())
            return real_build(*args, **kwargs)
        with patch('llm.events.bundle_store.build_bundle', side_effect=changed):
            self.error('catalog_changed', self.service.publish)
        self.assertEqual(self.count('bundle_publications'), 1)

    def test_atomic_file_failure_preserves_previous_output(self):
        self.ready()
        self.service.publish()
        self.output.write_text('previous export')
        with patch('llm.events.bundle_store.os.replace', side_effect=OSError('disk error')):
            self.error('storage_io_error', self.service.export_events, self.output)
        self.assertEqual(self.output.read_text(), 'previous export')
        self.assertEqual(list(self.output.parent.glob('.events.jsonl.*.tmp')), [])

    def test_published_tables_are_immutable(self):
        self.ready()
        self.service.publish()
        for table in ('bundle_publications', 'bundle_versions', 'bundle_publication_events'):
            for sql in ('DELETE FROM ' + table, 'UPDATE ' + table + ' SET event_id=event_id'
                        if table != 'bundle_publications' else
                        'UPDATE bundle_publications SET sequence=sequence'):
                with self.subTest(sql=sql), self.assertRaises(StorageError):
                    with self.store._transaction(write=True) as conn:
                        conn.execute(sql)
        self.assertEqual(self.count('bundle_publications'), 1)

    def test_migration_preserves_v3_and_backup_and_rollback(self):
        path = self.output.with_suffix('.sqlite3')
        with ExitStack() as stack:
            for name, value in [('DDL', V3_DDL), ('TABLES', V3_TABLES), ('SCHEMA_VERSION', 3)]:
                stack.enter_context(patch('llm.events.storage.' + name, value))
            old = EventStore(path)
            old_helper_store, old_helper_service = self.helper.store, self.helper.service
            self.helper.store = old
            self.helper.service = catalog_tests.CatalogService(old)
            aid = self.helper.publish()
            self.helper.initialize(aid)
            records = self.helper.service.list_events()
            source = old.get_current_article('a')
            self.helper.store, self.helper.service = old_helper_store, old_helper_service
        self.error('storage_migration_required', EventStore, path)
        with patch('llm.events.migration.B6_DDL', ['CREATE TABLE transient_b6(x)', 'INVALID SQL']):
            self.error('storage_error', migrate_v3_to_v4, path)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='transient_b6'").fetchone())
        result = migrate_v3_to_v4(path)
        reopened = EventStore(path)
        self.assertEqual(reopened.get_article(source.source_id), source)
        self.assertEqual(reopened.get_artifact(aid).artifact_id, aid)
        self.assertEqual(catalog_tests.CatalogService(reopened).list_events(), records)
        EventBundleService(reopened).publish()
        with closing(sqlite3.connect(result['backup_path'])) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM catalog_runs').fetchone()[0], 1)
        self.assertEqual(Path(result['backup_path']).stat().st_mode & 0o777, 0o600)
        self.error('unsupported_storage_schema', migrate_v3_to_v4, path)

    def test_cli(self):
        eid = self.ready()
        command = [sys.executable, '-m', 'llm.events.bundle_cli', '--db', str(self.store.path)]
        for args in (['publish'], ['get', eid], ['export', '--output', str(self.output)]):
            result = subprocess.run(command + args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            if args[0] == 'get':
                self.assertEqual(payload, self.service.get_event(eid))
        self.assertEqual(subprocess.run(command + ['get', 'missing'], capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run(command + ['export'], capture_output=True).returncode, 2)

    @unittest.skipUnless((ROOT / 'ranking/b6.py').is_file(), 'Ranking consumer is not present in this checkout')
    def test_ranking_version_boundary(self):
        eid = self.ready()
        self.service.publish()
        # The migrated consumer accepts both schemas without downgrading v0.2.
        from ranking.b6 import validate_bundle as ranking_validate
        v01 = json.loads((ROOT / 'docs/b6-examples/normal.json').read_text())
        v02 = self.service.get_event(eid)
        self.assertFalse([i for i in ranking_validate(v01) if i.severity == 'error'])
        self.assertFalse([i for i in ranking_validate(v02) if i.severity == 'error'])

    def test_quality_uses_structured_metadata(self):
        self.ready()
        captured = self.service._capture()
        record = captured['records'][0]
        context = captured['contexts'][0]
        def project(ctx=context, quality='complete', missing=(), decisions=(), owners=None):
            r = record.model_copy(deep=True)
            r.claims = [ctx.claim]
            return build_bundle(r, contexts=[ctx], b4_qualities={ctx.artifact_id: quality},
                missing_articles=missing, decisions=decisions, claim_events=owners,
                updated_at=record.updated_at)
        self.assertEqual(project().event.quality, 'complete')
        self.assertEqual(project(quality='partial').event.quality, 'partial')
        self.assertEqual(project(missing=['new-source']).event.quality, 'partial')
        changed = context.model_copy(deep=True)
        changed.claim.time.occurred_at = None
        self.assertEqual(project(ctx=changed).event.quality, 'partial')
        for field in ('action', 'object', 'modality', 'polarity'):
            changed = context.model_copy(deep=True)
            setattr(changed.claim, field, None if field in ('action', 'object') else 'unknown')
            self.assertEqual(project(ctx=changed).event.quality, 'partial', field)
        from llm.events.catalog_models import PairDecision
        cid = context.claim.claim_id
        decision = PairDecision(left_id=cid, right_id='other', identity='uncertain',
            rules_hash='offline', reasons=['uncertain'])
        self.assertEqual(project(decisions=[decision], owners={cid: record.event_id, 'other': 'another'}).event.quality, 'partial')
        decision.identity = 'same_event'
        self.assertEqual(project(decisions=[decision], owners={cid: record.event_id, 'other': 'another'}).event.quality, 'partial')
        # Opaque warning prose is preserved, but does not classify processing quality.
        record.limitations = ['B4 quality=partial is merely quoted here']
        self.assertEqual(project().event.quality, 'complete')

    def test_features_dates_unknown_labels_and_unicode_grounding(self):
        self.ready()
        captured = self.service._capture()
        record = captured['records'][0].model_copy(deep=True)
        contexts = captured['contexts']
        qualities = captured['qualities']
        kwargs = dict(contexts=contexts, b4_qualities=qualities, updated_at=record.updated_at)
        source = record.sources[0].model_copy(deep=True)
        source.source_id = 'second'
        source.article_id = 'second'
        # Lexical timestamp order disagrees with chronological order here.
        record.sources[0].published_at = '2026-10-20T10:00:00+03:00'
        source.published_at = '2026-10-20T08:00:00+00:00'
        record.sources.append(source)
        self.assertEqual(build_bundle(record, **kwargs).event.first_published_at, '2026-10-20T10:00:00+03:00')
        for s in record.sources:
            s.published_at = None
        projected = build_bundle(record, **kwargs)
        self.assertIsNone(projected.event.first_published_at)
        self.assertEqual(projected.event.quality, 'complete')
        record.event_type = 'other'
        record.claims[0].object = 'unlisted-object'
        record.claims[0].measurements[0].metric = 'unlisted-metric'
        hints = build_bundle(record, **kwargs).ranking_features
        self.assertEqual(hints.topics, ['other'])
        self.assertEqual(hints.factors, [])
        self.assertEqual(hints.industries, [])
        record.claims[0].object = 'steel'
        self.assertEqual(build_bundle(record, **kwargs).ranking_features.industries, ['steel'])
        # Prefixing a non-BMP character shifts spans by code points, not UTF-16 units.
        record = captured['records'][0].model_copy(deep=True)
        articles = [a.model_copy(deep=True) for a in captured['articles']]
        for article in articles:
            article.article.text = '😀 ' + article.article.text
        for span in record.evidence:
            if span.field == 'text':
                span.start += 2
                span.end += 2
        bundle = build_bundle(record, **kwargs)
        validate_grounding(bundle, articles)
        span = next(e for e in bundle.evidence if e.field == 'text')
        self.assertEqual(articles[0].article.text[span.start:span.end], span.quote)

    def test_source_correction_preserves_archive_and_updates_relation(self):
        eid = self.ready()
        self.service.publish()
        old = self.service.get_event(eid)
        text = 'Исправление: Банк России утвердил решение 2026-10-23: ключевая ставка составляет 15% в России.'
        raw = catalog_tests.case('a', text, [catalog_tests.measurement('15', '%')],
                                 entity='Банк России', expected={})
        artifact = self.helper.publish('a', value='15', raw=raw)
        self.error('catalog_stale', self.service.get_event, eid)
        self.catalog.apply_artifacts([artifact])
        self.service.publish()
        new = self.service.get_event(eid)
        self.assertEqual(new['event']['event_version'], 2)
        self.assertEqual(new['claims'][0]['measurements'][0]['value'], '15')
        self.assertEqual(new['relations'][0]['type'], 'updates')
        self.assertEqual(new['relations'][0]['to_id'], old['claims'][0]['claim_id'])
        self.assertNotIn(old['claims'][0]['claim_id'], {c['claim_id'] for c in new['claims']})
        self.assertGreater(new['sources'][0]['article_version'], old['sources'][0]['article_version'])
        with self.store._transaction() as conn:
            historical = json.loads(conn.execute('SELECT payload_json FROM bundle_versions '
                'WHERE event_id=? AND event_version=1', (eid,)).fetchone()[0])
        self.assertEqual(historical, old)
