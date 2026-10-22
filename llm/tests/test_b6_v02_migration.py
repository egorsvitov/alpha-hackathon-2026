"""B6 v0.2 ingestion, snapshot lifecycle and fixture analyst integration."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from ranking.b6 import load_bundles, validate_bundle
from ranking.contracts import (FixtureClock, InputError, EventVersionCollision,
                               parse_timestamp, payload_hash, validate_contract)
from llm.agent.tests.relevance_fixture import analyst_relevance
from ranking.event_store import EventStore, ingest_bundles
from llm.agent.context import build_context
from llm.agent.source_registry import build_source_registry
from llm.agent.generators.fixture import FixtureGenerator
from llm.agent.orchestrator import analyze_event
from llm.agent.ports import Services
from llm.agent.qa import validate_draft
from llm.agent.run_store import RunStore
from llm.rag.corpus import Corpus
from llm.rag.query import load_config

ROOT = Path(__file__).resolve().parents[2]


def example(name='normal'):
    return load_bundles(ROOT / f'docs/b6-examples/v0.2/{name}.json')[0]


def cutoff(b):
    return max(parse_timestamp(b['event'][key]) for key in ('updated_at', 'first_published_at')
               if b['event'][key] is not None) + timedelta(seconds=1)


class B6MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.profile = next(p for p in json.loads((ROOT / 'data/examples/personalization/profiles.json').read_text())['profiles']
                            if p['company_id'] == 'DEMO_TRADE')

    def errors(self, b):
        return [i for i in validate_bundle(b) if i.severity == 'error']

    def inputs(self, b):
        at = cutoff(b)
        with EventStore(':memory:') as store:
            ingest_bundles([b], store=store, clock=FixtureClock(at), source_mode='fixture')
            snapshot = store.list_latest(at)[0]
        return snapshot, analyst_relevance(snapshot, self.profile, at), at

    def test_all_v01_and_v02_examples(self):
        paths = list((ROOT / 'docs/b6-examples').glob('*.json')) + list((ROOT / 'docs/b6-examples/v0.2').glob('*.json'))
        self.assertEqual(len(paths), 11)
        for path in paths:
            with self.subTest(path=path):
                b = load_bundles(path)[0]
                self.assertEqual(self.errors(b), [])
                self.assertEqual(validate_contract(b, 'event-bundle'), [])

    def test_json_object_array_and_jsonl_preserve_payload(self):
        bundles = [example(), example('scenarios')]
        for name, raw in [('one.json', json.dumps(bundles[0])), ('batch.json', json.dumps(bundles)),
                          ('batch.jsonl', '\n'.join(json.dumps(b) for b in bundles))]:
            path = self.directory / name
            path.write_text(raw)
            self.assertEqual(load_bundles(path), bundles[:1] if name == 'one.json' else bundles)
        for raw in ['[{}, null]', '[[{}]]', '42']:
            path = self.directory / 'invalid.json'
            path.write_text(raw)
            with self.assertRaises(InputError):
                load_bundles(path)

    def test_versions_and_seconds_remain_strict(self):
        b = example()
        b['schema_version'] = '0.3'
        self.assertEqual(self.errors(b)[0].code, 'unsupported_contract_version')
        b = example()
        b['event']['first_published_at'] = '2026-10-20T10:00+03:00'
        self.assertTrue(self.errors(b))
        b = example()
        b['claims'][0]['measurements'][0]['value_role'] = 'current'
        self.assertTrue(self.errors(b))

    def test_scenario_and_role_evidence_coverage(self):
        b = example('scenarios')
        del b['claims'][0]['field_evidence']['/scenario']
        self.assertIn('field_evidence_uncovered', {i.code for i in self.errors(b)})
        b = example('before-after')
        del b['claims'][0]['measurements'][0]['value_role']
        self.assertTrue(self.errors(b))

    def test_active_claim_and_relation_semantics(self):
        b = example()
        b['claims'] = []
        self.assertIn('active_claims_required', {i.code for i in self.errors(b)})
        b = example()
        b['relations'] = [{'type': 'related_stage', 'target_kind': 'claim',
                           'from_id': b['claims'][0]['claim_id'], 'to_id': 'another-event'}]
        self.assertIn('relation_kind_invalid', {i.code for i in self.errors(b)})

    def test_store_reopen_preserves_every_new_field_and_hash(self):
        bundles = [example('scenarios'), example('before-after')]
        at = max(cutoff(b) for b in bundles)
        db = self.directory / 'fresh-v02.sqlite3'
        with EventStore(db) as store:
            receipts = ingest_bundles(bundles, store=store, clock=FixtureClock(at), source_mode='fixture')
            self.assertTrue(all(r.status == 'inserted' for r in receipts))
            repeats = ingest_bundles(bundles, store=store, clock=FixtureClock(at), source_mode='fixture')
            self.assertTrue(all(r.status == 'unchanged' for r in repeats))
        with EventStore(db) as store:
            for b in bundles:
                stored = store.get_latest(b['event']['event_id'], at)
                self.assertEqual(stored.bundle, b)
                self.assertEqual(stored.payload_sha256, payload_hash(b))

    def test_schema_change_does_not_silently_overwrite_legacy_version(self):
        old = load_bundles(ROOT / 'docs/b6-examples/normal.json')[0]
        new = deepcopy(old)
        new['schema_version'] = '0.2'
        for c in new['claims']:
            c['scenario'] = None
            for i, m in enumerate(c['measurements']):
                m['value_role'] = 'reported'
                c['field_evidence'][f'/measurements/{i}/value_role'] = c['evidence_ids']
        self.assertFalse(self.errors(new))
        at = cutoff(old)
        with EventStore(':memory:') as store:
            store.put(old, received_at=at, source_mode='fixture')
            with self.assertRaises(EventVersionCollision):
                store.put(new, received_at=at, source_mode='fixture')
            self.assertEqual(store.get_latest(old['event']['event_id'], at).bundle, old)

    def test_withdrawal_does_not_resurrect_old_version(self):
        b = example()
        at = cutoff(b)
        withdrawn = deepcopy(b)
        withdrawn['event'].update(event_version=2, availability='withdrawn', quality='insufficient', first_published_at=None)
        for key in ('claims', 'sources', 'evidence', 'conflicts', 'relations'):
            withdrawn[key] = []
        with EventStore(':memory:') as store:
            ingest_bundles([b, withdrawn], store=store, clock=FixtureClock(at), source_mode='fixture')
            latest = store.list_latest(at)
            self.assertEqual(latest[0].event_version, 2)
            rel = analyst_relevance(latest[0], self.profile, at)
            with self.assertRaisesRegex(ValueError, 'event_unavailable'):
                build_context(latest[0], self.profile, rel, run_id='withdrawn', role='cfo', as_of=at)

    def test_v02_scenarios_reach_analyst_without_rewriting(self):
        b = example('scenarios')
        snap, rel, at = self.inputs(b)
        registry = build_source_registry(snap)
        context = build_context(snap, self.profile, rel, run_id='scenario-check', role='cfo', as_of=at, registry=registry)
        self.assertEqual(context.payload['claims'], b['claims'])
        self.assertEqual(rel['evidence_status'], 'limited')
        with RunStore(':memory:') as runs:
            config = load_config()
            services = Services(generator=FixtureGenerator(), run_store=runs,
                                corpus=Corpus(self.directory / 'absent-corpus.jsonl', config=config),
                                config={'mode': 'fixture', 'rag': config}, clock=FixtureClock(at))
            result = analyze_event(snap, self.profile, rel, role='cfo', as_of=at, services=services)
            self.assertEqual(result['status'], 'limited')
            self.assertEqual([c['text'] for c in result['claims'] if c['kind'] == 'source_fact'], [c['text'] for c in b['claims']])
            self.assertEqual(runs.get_run(result['run_id'])['context']['snapshot']['bundle'], b)
            from llm.agent.questions import answer_question
            with EventStore(':memory:') as current:
                ingest_bundles([b], store=current, clock=FixtureClock(at), source_mode='fixture')
                services.event_store = current
                answer = answer_question(result['run_id'], 'Каких данных не хватает?',
                                         company_id=self.profile['company_id'], role='cfo', services=services)
                self.assertEqual(answer['status'], 'limited')
                self.assertEqual(answer['parent_run_id'], result['run_id'])


    def test_numerical_role_inference_and_unconditional_scenario_are_blocked(self):
        from llm.agent.ports import GenerationRequest
        b = example('scenarios')
        # Synthetic derivative: exercise numeric-role guard within a known
        # rankable policy_rate event. No claim is made about financial truth.
        b['claims'][0]['measurements'][0]['value_role'] = 'before'
        snap, rel, at = self.inputs(b)
        registry = build_source_registry(snap)
        context = build_context(snap, self.profile, rel, run_id='guard-check', role='cfo', as_of=at, registry=registry)
        request = GenerationRequest('interpret', 'guard-check', 'g', 'offline-fixture-v1', 'test', context.payload, {})
        draft = FixtureGenerator().generate(request).payload
        inference = next(c for c in draft['claims'] if c['kind'] == 'ai_inference')
        inference['input_claim_ids'] = [b['claims'][0]['claim_id']]
        inference['source_ids'] = [sid for sid, s in registry.items() if s['evidence_id'] in b['claims'][0]['evidence_ids']]
        inference['support_status'] = 'supported'
        inference['assumptions'] = []
        inference['text'] = 'Текущая ставка составляет 13,75%.'
        codes = {i['code'] for i in validate_draft(draft, context, registry).issues}
        self.assertIn('scenario_inference_unconditional', codes)
        self.assertIn('measurement_role_inference_unreviewed', codes)


if __name__ == '__main__':
    unittest.main()
