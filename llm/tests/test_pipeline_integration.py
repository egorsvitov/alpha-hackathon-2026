"""Real local RAG → pinned context → fixture analysis → persisted dialogue."""
from copy import deepcopy
from dataclasses import replace
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ranking.b6 import load_bundles
from ranking.contracts import FixtureClock, parse_timestamp, require_valid_contract
from llm.agent.tests.relevance_fixture import analyst_relevance
from ranking.event_store import EventStore, ingest_bundles
from llm.rag.corpus import Corpus
from llm.rag.retriever import retrieve_evidence
from llm.agent.generators.fixture import FixtureGenerator
from llm.agent.orchestrator import analyze_event, answer_question
from llm.agent.ports import Services
from llm.agent.run_store import RunStore


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'llm/rag/tests/fixtures/corpus.jsonl'
PROFILES = ROOT / 'data/examples/personalization/profiles.json'
AS_OF = '2026-10-20T18:00:00+03:00'


class PipelineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.profile = next(p for p in json.loads(PROFILES.read_text())['profiles']
                            if p['company_id'] == 'DEMO_TRADE')
        self.as_of = parse_timestamp(AS_OF)
        self.events_path = self.directory / 'events.sqlite3'
        self.events = EventStore(self.events_path)
        self.addCleanup(self.events.close)
        bundles = load_bundles(ROOT / 'docs/b6-examples/forecast.json')
        ingest_bundles(bundles, store=self.events,
                       clock=FixtureClock('2026-10-20T17:00:00+03:00'), source_mode='fixture')
        self.snapshot = self.events.list_latest(as_of=self.as_of)[0]
        self.relevance = analyst_relevance(self.snapshot, self.profile, self.as_of)
        self.corpus = Corpus(CORPUS)
        self.run_path = self.directory / 'runs.sqlite3'
        self.runs = RunStore(self.run_path)
        self.addCleanup(self.runs.close)

    def services(self, **overrides):
        values = dict(event_store=self.events, corpus=self.corpus, run_store=self.runs,
                      retriever=retrieve_evidence, generator=FixtureGenerator(),
                      config={'mode': 'fixture'}, clock=FixtureClock(self.as_of))
        values.update(overrides)
        return Services(**values)

    def analyze(self, **overrides):
        return analyze_event(self.snapshot, self.profile, self.relevance,
                             role='cfo', as_of=self.as_of, services=self.services(**overrides))

    def test_real_retriever_and_analysis_keep_forecast_and_provenance(self):
        evidence = retrieve_evidence(self.snapshot.bundle, self.profile, self.relevance,
                                     as_of=self.as_of, corpus=self.corpus, config={})
        require_valid_contract(evidence, 'evidence-bundle')
        self.assertTrue(evidence['hits'])
        for hit in evidence['hits']:
            self.corpus.verify_hit(hit)
        result = self.analyze()
        self.assertEqual(result['status'], 'limited', result.get('limitations'))
        self.assertEqual(result['trace']['corpus_version'], evidence['corpus_version'])
        self.assertEqual(result['trace']['qa']['semantic_status'], 'not_run')
        self.assertEqual(result['usage']['pricing_status'], 'no_llm_calls')
        original = self.snapshot.bundle['claims'][0]
        reports = [c for c in result['claims'] if c['kind'] == 'source_fact']
        self.assertTrue(reports)
        self.assertEqual(reports[0]['text'], original['text'])
        self.assertEqual(reports[0]['modality'], 'forecast')
        self.assertTrue(any(c['kind'] == 'ai_inference' for c in result['claims']))
        self.assertFalse(any(c['kind'] == 'bank_view' for c in result['claims']))
        self.assertEqual(self.runs.get_run(result['run_id'])['result'], result)

    def test_corrupted_bank_quote_blocks_before_generation(self):
        generator = FixtureGenerator()
        def corrupt(*args, **kwargs):
            evidence = retrieve_evidence(*args, **kwargs)
            self.assertTrue(evidence['hits'])
            evidence['hits'][0]['quote'] += ' Подменённый текст.'
            return evidence
        result = self.analyze(retriever=corrupt, generator=generator)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(result['claims'], [])
        self.assertEqual(generator.requests, [])

    def test_foreign_evidence_profile_blocks_before_generation(self):
        generator = FixtureGenerator()
        def foreign(*args, **kwargs):
            evidence = retrieve_evidence(*args, **kwargs)
            evidence['profile_version'] = 'foreign-version'
            return evidence
        result = self.analyze(retriever=foreign, generator=generator)
        self.assertEqual(result['status'], 'insufficient_evidence')
        self.assertEqual(generator.requests, [])

    def test_corpus_version_and_source_metadata_cannot_be_relabelled(self):
        for mutation in ('corpus_version', 'published_at', 'verification_status', 'evidence_id', 'is_synthetic'):
            with self.subTest(mutation=mutation):
                generator = FixtureGenerator()
                def corrupt(*args, **kwargs):
                    evidence = retrieve_evidence(*args, **kwargs)
                    if mutation == 'corpus_version':
                        evidence['corpus_version'] = '0' * 64
                    else:
                        evidence['hits'][0][mutation] = {
                            'published_at':'2026-10-18', 'verification_status':'visually_checked_excerpt',
                            'evidence_id':'bank:' + '0' * 64, 'is_synthetic':False,
                        }[mutation]
                    return evidence
                result = self.analyze(retriever=corrupt, generator=generator)
                self.assertEqual(result['status'], 'insufficient_evidence')
                self.assertEqual(generator.requests, [])
                artifacts = self.runs.get_run(result['run_id'])['artifacts']
                self.assertTrue(any(a['kind'] == 'retrieval_result_unvalidated' for a in artifacts))

    def test_context_hit_limit_really_removes_bank_sources(self):
        generator = FixtureGenerator()
        result = self.analyze(generator=generator, config={'mode':'fixture','max_bank_hits':0})
        self.assertEqual(result['status'], 'limited')
        self.assertEqual(generator.requests[0].structured_payload['bank_sources'], [])
        self.assertFalse(any(s['type'] == 'bank_analytics' for s in result['sources']))

    def test_actual_retry_is_bounded_and_query_changes(self):
        plans = []
        def record(*args, **kwargs):
            evidence = retrieve_evidence(*args, **kwargs)
            plans.append(deepcopy(evidence['query_plan'][0]))
            return evidence
        generator = FixtureGenerator(['request_evidence'])
        result = self.analyze(retriever=record, generator=generator)
        self.assertEqual(result['status'], 'limited')
        self.assertEqual(len(plans), 2)
        self.assertEqual([p['attempt'] for p in plans], [0, 1])
        self.assertTrue(plans[1]['retry_reason'])
        self.assertNotEqual(plans[0]['queries'], plans[1]['queries'])
        self.assertEqual(len(generator.requests), 2)

    def test_restart_followup_child_keeps_parent_and_company_scope(self):
        parent = self.analyze()
        # Independent connection represents a reopened service without deleting original data.
        with RunStore(self.run_path) as restarted:
            services = self.services(run_store=restarted)
            answer = answer_question(parent['run_id'], 'Каких условий кредита не хватает?',
                                     company_id='DEMO_TRADE', role='cfo', services=services)
            self.assertEqual(answer['status'], 'limited', answer.get('limitations'))
            self.assertEqual(answer['parent_run_id'], parent['run_id'])
            self.assertNotEqual(answer['run_id'], parent['run_id'])
            self.assertEqual(restarted.get_run(answer['run_id'])['parent_run_id'], parent['run_id'])
            self.assertEqual(restarted.get_run(parent['run_id'])['result'], parent)
            with self.assertRaises(PermissionError):
                answer_question(parent['run_id'], 'Вопрос', company_id='DEMO_IT', role='cfo', services=services)

    def test_stale_payload_hash_cannot_be_silently_reused(self):
        changed = deepcopy(self.snapshot.bundle)
        changed['event']['title'] += ' Изменено после загрузки.'
        with self.assertRaises(ValueError):
            analyze_event(replace(self.snapshot, bundle=changed), self.profile, self.relevance,
                          role='cfo', as_of=self.as_of, services=self.services())

    def test_loan_and_deposit_templates_use_actual_profile_features(self):
        from ranking.profiles import CompanyRepository
        source = ROOT / 'ranking/tests/fixtures/profiles/companies.csv'
        with source.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream, delimiter=';')
            columns = reader.fieldnames
            base = next(row for row in reader if row['SYNTHETIC_U'] == 'DEMO_TRADE')
        results = []
        for product, flag in [('loan', 'CL_PRODUCT_LOA_MARK'), ('deposit', 'CL_PRODUCT_DEP_MARK')]:
            row = dict(base)
            row['CL_PRODUCT_LOA_MARK'] = row['CL_PRODUCT_DEP_MARK'] = '0'
            row[flag] = '1'
            csv_path = self.directory / (product + '.csv')
            with csv_path.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=columns, delimiter=';')
                writer.writeheader()
                writer.writerow(row)
            profile = CompanyRepository(csv_path, imported_at=AS_OF, is_synthetic=True).get('DEMO_TRADE')
            relevance = analyst_relevance(self.snapshot, profile, self.as_of)
            result = analyze_event(self.snapshot, profile, relevance, role='cfo', as_of=self.as_of,
                                   services=self.services())
            self.assertEqual(result['status'], 'limited')
            why = next(c for c in result['claims'] if c['section'] == 'why_important')
            self.assertIn('DEMO_TRADE:/products/' + product, why['feature_ids'])
            self.assertEqual(why['support_status'], 'conditional')
            self.assertTrue(why['assumptions'])
            results.append(result)
        source_texts = lambda result: [c['text'] for c in result['claims'] if c['kind'] == 'source_fact']
        self.assertEqual(source_texts(results[0]), source_texts(results[1]))
        why_text = lambda result: next(c['text'] for c in result['claims'] if c['section'] == 'why_important')
        self.assertNotEqual(why_text(results[0]), why_text(results[1]))

    def test_cli_analysis_and_followup_in_separate_processes(self):
        state = self.directory / 'cli-state'
        relevance_path = self.directory / 'relevance.json'
        relevance_path.write_text(json.dumps(self.relevance))
        common = ['--company-id','DEMO_TRADE','--role','cfo','--mode','fixture',
                  '--state-dir',str(state),'--corpus',str(CORPUS)]
        analysis = subprocess.run(
            [sys.executable,'-m','llm.agent','analyze',*common,
             '--event',str(ROOT/'docs/b6-examples/forecast.json'),'--profiles',str(PROFILES),'--as-of',AS_OF,
             '--relevance',str(relevance_path)],
            cwd=ROOT,text=True,capture_output=True,timeout=45)
        self.assertEqual(analysis.returncode, 0, analysis.stdout + analysis.stderr)
        result = json.loads(analysis.stdout)
        self.assertEqual(result['status'], 'limited')
        followup = subprocess.run(
            [sys.executable,'-m','llm.agent','ask',*common,'--run-id',result['run_id'],
             '--question','Каких данных о кредите не хватает?'],
            cwd=ROOT,text=True,capture_output=True,timeout=45)
        self.assertEqual(followup.returncode, 0, followup.stdout + followup.stderr)
        answer = json.loads(followup.stdout)
        self.assertEqual(answer['status'], 'limited', answer.get('limitations'))
        self.assertEqual(answer['parent_run_id'], result['run_id'])


if __name__ == '__main__':
    unittest.main()
