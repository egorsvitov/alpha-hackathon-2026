"""Regression checks for public history, semantics and atomic export fencing."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from llm.events.tests import test_bundle as fixtures
from llm.events import PublicationConfig, EventStore, StorageError
from llm.events.publication.builder import build_bundle
from llm.events.publication.cli import schemas
from llm.events.publication.export import _write
from llm.events.publication.features import resolved_rules, derive_features, assess_quality, validate_feature_trace
from llm.events.publication.models import BundleProjection, PublicationManifest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[3]


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BundleTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.service = self.fixture.service
        self.eid = self.fixture.ready()
        self.root = self.fixture.output.parent

    def error(self, code, fn, *args, **kwargs):
        with self.assertRaises(StorageError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def project(self, record=None):
        captured = self.service._capture()
        record = record or captured['records'][0]
        return build_bundle(record, contexts=captured['contexts'],
            b4_qualities=captured['qualities'], updated_at=record.updated_at)

    def edit_article(self):
        raw = self.store.get_current_article('a').article.model_dump(mode='json')
        raw['title'] += ' исправление'
        # A second connection represents an independent importing process.
        other = EventStore(self.store.path, clock=self.store.clock)
        other.import_batch(json.dumps([raw]).encode())

    def test_exact_labels_and_feature_trace(self):
        record = self.service._capture()['records'][0]
        record.event_type = 'sanction_or_trade_restriction'
        c = record.claims[0]
        c.object = 'import_tariff'
        c.action = 'introduce'
        c.measurements[0].metric = 'import_tariff'
        projected = self.project(record)
        self.assertEqual(projected.ranking_features.topics, ['trade'])
        self.assertNotIn('sanctions', projected.ranking_features.factors)
        record.event_type = 'company_results'
        c.object = c.measurements[0].metric = 'revenue'
        self.assertEqual(self.project(record).ranking_features.topics, ['other'])
        self.assertEqual(self.project(record).ranking_features.industries, [])
        c.object = 'sanctions'
        self.assertIn('sanctions', self.project(record).ranking_features.topics)
        # Every copied or derived label has a field basis or an explicit classification basis.
        features, traces = derive_features(record, resolved_rules(PublicationConfig()))
        bundle = self.project(record)
        bundle.ranking_features = features
        validate_feature_trace(BundleProjection(bundle=bundle, feature_trace=traces, quality_reasons=[]))
        classification = [b for t in traces for b in t.bases if b.classification]
        self.assertTrue(all(not b.evidence_ids for b in classification))
        broken = deepcopy(traces)
        broken[0].bases = []
        self.error('invalid_feature_trace', validate_feature_trace,
            BundleProjection(bundle=bundle, feature_trace=broken, quality_reasons=[]))

    def test_quality_and_modality_titles(self):
        captured = self.service._capture()
        record = captured['records'][0]
        c = record.claims[0]
        c.action = c.object = None
        c.measurements = []
        self.assertEqual(self.project(record).event.quality, 'insufficient')
        record = captured['records'][0] = self.service._capture()['records'][0]
        c = record.claims[0]
        c.modality = 'forecast'
        self.assertTrue(self.project(record).event.title.startswith('Прогноз:'))
        self.assertNotIn('policy_rate', self.project(record).event.title)
        self.assertEqual(self.project(record).event.quality, 'partial')
        c.time.forecast_horizon = c.time.occurred_at
        c.polarity = 'negated'
        self.assertTrue(self.project(record).event.title.startswith('Прогноз:'))
        c.modality = 'opinion'
        self.assertTrue(self.project(record).event.title.startswith('Оценка:'))
        second = c.model_copy(deep=True)
        second.claim_id = 'second'
        second.modality = 'asserted'
        record.claims.append(second)
        self.assertTrue(self.project(record).event.title.startswith('Сведения:'))

    def test_history_changes_provenance_and_stale_reads(self):
        first = self.service.publish_snapshot()
        old = self.service.get_event(self.eid)
        dictionary = resolved_rules(PublicationConfig()).model_copy(deep=True)
        dictionary.category_titles['policy_rate'] = 'Решение о ставке'
        second = self.service.publish_snapshot(PublicationConfig(dictionary=dictionary))
        self.assertGreater(second['publication_revision'], first['publication_revision'])
        self.assertEqual(self.service.get_event(self.eid)['event']['event_version'], 2)
        self.assertEqual(self.service.get_event(self.eid, event_version=1), old)
        self.assertEqual(self.service.get_event(self.eid, publication_revision=1), old)
        changes = self.service.changes_since(1)
        self.assertEqual([b['event']['event_version'] for b in changes['events']], [2])
        self.assertEqual(self.service.changes_since(2)['events'], [])
        provenance = self.service.get_provenance(self.eid, 1)
        self.assertTrue(provenance['artifact_ids'])
        self.assertTrue(provenance['feature_trace'])
        self.assertEqual(provenance['catalog_event_version'], 1)
        self.assertEqual(provenance['semantic_support'], 'not_assessed')
        self.error('invalid_argument', self.service.get_event, self.eid,
            event_version=1, publication_revision=1)
        self.error('publication_not_found', self.service.export_events, publication_revision=999)
        self.edit_article()
        self.error('catalog_stale', self.service.get_event, self.eid)
        self.assertEqual(self.service.get_event(self.eid, event_version=1), old)
        self.assertEqual(self.service.export_events(publication_revision=1), [old])
        self.assertEqual(self.service.get_provenance(self.eid, 1), provenance)

    def test_plan_limit_and_config_pin_are_read_only(self):
        counts = (self.fixture.count('bundle_versions'), self.fixture.count('bundle_publications'))
        plan = self.service.plan_publication()
        self.assertEqual(plan['changed_event_ids'], [self.eid])
        self.assertEqual(counts, (self.fixture.count('bundle_versions'), self.fixture.count('bundle_publications')))
        self.service.publish_snapshot()
        self.assertEqual(self.service.plan_publication()['changed_event_ids'], [])
        self.assertEqual(self.service.publish_snapshot()['status'], 'cached')
        # A second event exceeds an explicit small limit, without publishing part of the snapshot.
        artifact = self.fixture.helper.publish('b', day='2026-10-24')
        self.fixture.catalog.apply_artifacts([artifact])
        self.error('publication_limit_exceeded', self.service.publish_snapshot,
            PublicationConfig(max_events=1))
        self.assertEqual(self.fixture.count('bundle_publications'), 1)

    def test_export_manifest_checksum_and_history(self):
        self.service.publish_snapshot()
        path = self.root / 'snapshot'
        receipt = self.service.export_snapshot(path)
        self.assertEqual(receipt['publication_revision'], 1)
        manifest = json.loads((path / 'manifest.json').read_text())
        PublicationManifest.model_validate(manifest)
        self.assertTrue(manifest['complete_snapshot'])
        data = (path / 'events.jsonl').read_bytes()
        self.assertEqual(manifest['events_sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(manifest['count'], len(data.splitlines()))
        self.error('export_exists', self.service.export_snapshot, path)
        self.assertEqual((path / 'events.jsonl').read_bytes(), data)
        filtered = self.root / 'filtered'
        self.service.export_snapshot(filtered, include_withdrawn=False)
        self.assertFalse(json.loads((filtered / 'manifest.json').read_text())['complete_snapshot'])
        self.edit_article()
        self.service.export_snapshot(self.root / 'history', publication_revision=1)

    def test_export_article_race_preserves_previous_file_and_no_partial_directory(self):
        self.service.publish_snapshot()
        path = self.root / 'events.jsonl'
        path.write_text('previous')
        def writing(file, data):
            _write(file, data)
            if file.name == 'manifest.json':
                self.edit_article()
        with patch('llm.events.publication.export._write', side_effect=writing):
            self.error('catalog_stale', self.service.export_events, path)
        self.assertEqual(path.read_text(), 'previous')
        self.assertEqual(list(self.root.glob('.events.jsonl.*.tmp')), [])

    def test_export_publication_race_and_io_failure(self):
        self.service.publish_snapshot()
        path = self.root / 'snapshot'
        def writing(file, data):
            _write(file, data)
            if file.name == 'manifest.json':
                self.service.publish_snapshot(PublicationConfig(max_events=10001))
        with patch('llm.events.publication.export._write', side_effect=writing):
            self.error('publication_changed', self.service.export_snapshot, path)
        self.assertFalse(path.exists())
        with patch('llm.events.publication.export.os.replace', side_effect=OSError('disk full')):
            self.error('storage_io_error', self.service.export_snapshot, path)
        self.assertFalse(path.exists())
        self.assertEqual(list(self.root.glob('.snapshot.*.tmp')), [])

    def test_manual_feature_expectations_cover_every_category(self):
        from typing import get_args
        from llm.events.contracts.models import EventType
        fixture = json.loads((ROOT / 'llm/events/fixtures/publication.b6.1.json').read_text())
        seen = set()
        for case in fixture['cases']:
            with self.subTest(case=case):
                record = self.service._capture()['records'][0]
                record.event_type = case['event_type']
                seen.add(record.event_type)
                record.claims[0].object = case['object']
                record.claims[0].measurements[0].metric = case['metric']
                features, _ = derive_features(record, resolved_rules(PublicationConfig()))
                for field in ('topics', 'factors', 'industries'):
                    self.assertEqual(getattr(features, field), case[field])
        self.assertEqual(seen, set(get_args(EventType)))

    def test_pinned_dictionary_and_historical_provenance_do_not_recompute(self):
        self.service.publish_snapshot()
        old = self.service.get_event(self.eid)
        provenance = self.service.get_provenance(self.eid, 1)
        original = Path.read_text
        def changed(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            if path.name == 'publication_vocabulary.json':
                data = json.loads(text)
                data['category_titles']['policy_rate'] = 'Новая метка ставки'
                return json.dumps(data, ensure_ascii=False)
            return text
        with patch.object(Path, 'read_text', changed):
            self.assertEqual(self.service.publish_snapshot()['status'], 'cached')
            self.service.publish(rebuild=True)
            self.assertEqual(self.service.get_event(self.eid)['event']['event_version'], 2)
            self.assertEqual(self.service.get_event(self.eid, event_version=1), old)
            self.assertEqual(self.service.get_provenance(self.eid, 1), provenance)

    def test_public_pure_builder_and_consumer(self):
        from llm.events import build_bundle as public_builder, EventBundle, PublicationContext
        from llm.events.examples.read_bundles import read_usable
        captured = self.service._capture()
        record = captured['records'][0]
        context = PublicationContext(articles=captured['articles'],
            normalized_inputs=captured['normalized_inputs'])
        bundle = public_builder(record, context, PublicationConfig(), public_version=8,
            published_at=record.updated_at)
        self.assertIsInstance(bundle, EventBundle)
        self.assertEqual(bundle.event.event_version, 8)
        self.service.publish_snapshot()
        receipt = self.service.export_snapshot(self.root / 'consumer')
        usable = list(read_usable(Path(receipt['output_path']) / 'events.jsonl'))
        self.assertEqual(usable[0][0], (self.eid, 1))
        self.assertEqual(usable[0][1].claims, bundle.claims)

    def test_withdrawal_change_feed_and_historical_provenance(self):
        self.service.publish_snapshot()
        self.edit_article()
        self.fixture.catalog.rebuild_catalog()
        self.service.publish_snapshot()
        changes = self.service.changes_since(1)
        self.assertEqual(changes['events'][0]['event']['availability'], 'withdrawn')
        self.assertEqual(changes['events'][0]['event']['quality'], 'insufficient')
        self.assertEqual(self.service.export_events(include_withdrawn=False), [])
        self.error('invalid_argument', self.service.export_events, include_withdrawn=0)
        old = self.service.get_event(self.eid, event_version=1)
        self.assertEqual(old['event']['availability'], 'active')
        self.assertEqual(self.service.get_provenance(self.eid, 1)['publication_revision'], 1)

    def test_internal_schemas_and_cli_history(self):
        for name, schema in schemas().items():
            Draft202012Validator.check_schema(schema)
            saved = json.loads((ROOT / 'docs/contracts' / name).read_text())
            saved.pop('$schema')
            self.assertEqual(saved, schema)
        dictionary = resolved_rules(PublicationConfig())
        self.assertEqual(len({r.rule_id for r in dictionary.rules}), len(dictionary.rules))
        self.service.publish_snapshot()
        base = [sys.executable, '-m', 'llm.events.publication.cli', '--db', str(self.store.path)]
        for args in (['plan'], ['get', self.eid, '--event-version', '1'], ['changes', '--since', '1'],
                     ['provenance', self.eid, '--event-version', '1'], ['schema', '--check'],
                     ['export', '--output', str(self.root / 'cli')]):
            result = subprocess.run(base + args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            json.loads(result.stdout)


if __name__ == '__main__':
    unittest.main()
