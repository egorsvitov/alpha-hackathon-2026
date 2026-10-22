import json
import tempfile
import unittest
from pathlib import Path
from llm.agent.ports import Services, QAReport
from llm.agent.run_store import RunStore
from llm.agent.orchestrator import analyze_event
from llm.agent.generators.fixture import FixtureGenerator
from llm.rag.retriever import load_config,retrieve_evidence
from llm.rag.corpus import Corpus
from ranking.contracts import FixtureClock
from llm.agent.tests.test_context import fixture_inputs,ROOT

class OrchestratorTests(unittest.TestCase):
    def run_fixture(self, faults=(),qa=None,config=None):
        s,p,r,at,_=fixture_inputs()
        store=RunStore(':memory:');rag=load_config();corpus=Corpus('/nonexistent/synthetic-test-corpus',config=rag)
        gen=FixtureGenerator(faults)
        services=Services(generator=gen,qa=qa,run_store=store,retriever=retrieve_evidence,corpus=corpus,
            config=config or {'mode':'fixture','rag':rag},clock=FixtureClock(at))
        result=analyze_event(s,p,r,role='cfo',as_of=at,services=services)
        self.addCleanup(store.close)
        return result,gen,store
    def test_success_is_limited_and_persisted(self):
        result,gen,store=self.run_fixture()
        self.assertEqual(result['schema_version'],'0.2');self.assertEqual(result['status'],'limited')
        self.assertEqual(result['usage']['pricing_status'],'no_llm_calls')
        self.assertEqual(result['trace']['qa']['semantic_status'],'not_run')
        self.assertEqual(store.get_run(result['run_id'])['result'],result)
        self.assertEqual(result['trace']['steps'][-1]['state'],'completed')
    def test_foreign_ref_repaired_bounded(self):
        result,gen,_=self.run_fixture(['foreign_source'])
        self.assertEqual(len(gen.requests),2);self.assertEqual(result['status'],'limited')
    def test_repeated_failure_never_publishes(self):
        result,gen,_=self.run_fixture(['forecast_drift','foreign_source'])
        self.assertEqual(len(gen.requests),2);self.assertEqual(result['status'],'insufficient_evidence')
        self.assertFalse(result['claims']);self.assertEqual(result['trace']['steps'][-1]['state'],'stopped')
    def test_retrieval_limit_also_limited_by_generations(self):
        def qa(*args):
            return QAReport(status='failed',next_action='retrieve_more',structural_status='failed',publication_status='block',missing_evidence_queries=['different query'])
        result,gen,_=self.run_fixture(qa=qa)
        self.assertEqual(result['trace']['retrieval_count'],2)
        self.assertEqual(result['trace']['generation_count'],2)
        self.assertEqual(result['status'],'insufficient_evidence')
    def test_live_closed_without_reading_keys(self):
        result,gen,_=self.run_fixture(config={'mode':'live','rag':load_config()})
        self.assertEqual(result['status'],'provider_unavailable');self.assertEqual(len(gen.requests),0)
    def test_full_forecast_draft_contract_and_qa(self):
        from llm.agent.context import RunContext
        from llm.agent.qa import validate_draft
        c=RunContext(**json.loads((ROOT/'data/examples/personalization/agent/forecast-context.json').read_text()))
        d=json.loads((ROOT/'data/examples/personalization/agent/forecast-draft.json').read_text())
        report=validate_draft(d,c,c.registry)
        self.assertEqual(report.structural_status,'passed');self.assertEqual(report.publication_status,'limited');self.assertFalse(report.issues)
    def test_four_b6_modes_preserve_source_claims_and_states(self):
        from ranking.contracts import StoredEvent,payload_hash
        from llm.agent.tests.relevance_fixture import analyst_relevance
        for name in ('normal','forecast','conflict','partial'):
            with self.subTest(name=name):
                _,p,_,at,_=fixture_inputs()
                bundle=json.loads((ROOT/f'docs/b6-examples/{name}.json').read_text())
                if name=='partial':
                    # Explicit synthetic derivative for the dated metadata path.
                    bundle['event']['event_id']='synthetic:dated-partial'
                    bundle['event']['first_published_at']='2026-10-20T10:00:00+03:00'
                    for source in bundle['sources']:
                        source['article_id']+=':dated-synthetic-derivative'
                        source['published_at']=bundle['event']['first_published_at']
                    bundle['limitations'].append('Synthetic derivative supplies publication metadata only; claim details remain partial.')
                from ranking.b6 import require_valid_bundle
                require_valid_bundle(bundle)
                snap=StoredEvent(bundle['event']['event_id'],1,payload_hash(bundle),at,'fixture','synthetic-test',bundle)
                rel=analyst_relevance(snap,p,at)
                rag=load_config();store=RunStore(':memory:');self.addCleanup(store.close)
                services=Services(generator=FixtureGenerator(),run_store=store,retriever=retrieve_evidence,
                    corpus=Corpus('/nonexistent/synthetic-corpus',config=rag),config={'mode':'fixture'},clock=FixtureClock(at))
                result=analyze_event(snap,p,rel,role='cfo',as_of=at,services=services)
                facts=[c for c in result['claims'] if c['kind']=='source_fact']
                self.assertEqual(result['status'],'limited')
                self.assertEqual([c['text'] for c in facts],[c['text'] for c in bundle['claims']])
                self.assertEqual([c['modality'] for c in facts],[c['modality'] for c in bundle['claims']])
                self.assertEqual(result['section_states']['bank_view']['status'],'insufficient_evidence')
                if name=='conflict':
                    self.assertEqual(len(facts),2)
                    saved=store.get_run(result['run_id'])
                    contexts=[a['payload'] for a in saved['artifacts'] if a['kind']=='context']
                    self.assertEqual(contexts[-1]['payload']['conflicts'],bundle['conflicts'])

    def test_missing_mode_is_closed(self):
        result,gen,_=self.run_fixture(config={'rag':load_config()})
        self.assertEqual(result['status'],'provider_unavailable');self.assertEqual(len(gen.requests),0)
