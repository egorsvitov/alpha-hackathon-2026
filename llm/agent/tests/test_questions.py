from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from ranking.contracts import FixtureClock
from llm.agent.ports import Services
from llm.agent.generators.fixture import FixtureGenerator
from llm.agent.questions import answer_question
from llm.agent.run_store import RunStore

class QuestionsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'runs.sqlite'
        self.context=json.loads((Path(__file__).parent/'fixtures/qa_history/forecast.json').read_text())['context']
        with RunStore(self.path) as store:
            store.create_run('parent',self.context);store.finish_run('parent',{'status':'limited'})
        self.store=RunStore(self.path)
        self.current=SimpleNamespace(**deepcopy(self.context['snapshot']))
        self.calls=[]
        self.services=Services(run_store=self.store,generator=FixtureGenerator(),config={'mode':'fixture','question_extra_search':False},clock=FixtureClock('2026-10-21T12:00:00+03:00'),event_store=SimpleNamespace(get_latest=lambda *args:self.current))
    def tearDown(self):self.store.close();self.temp.cleanup()
    def ask(self,**kwargs):
        return answer_question('parent','У нас фиксированная ставка. Что уточнить?',company_id=kwargs.get('company_id','DEMO_TRADE'),role=kwargs.get('role','cfo'),services=self.services)
    def test_restart_child_scope_assumption_parent_immutable(self):
        before=self.store.get_run('parent');answer=self.ask()
        self.assertEqual(answer['status'],'limited');self.assertFalse(answer['did_extra_search']);self.assertNotEqual(answer['run_id'],'parent')
        self.assertEqual(self.store.get_run(answer['run_id'])['parent_run_id'],'parent')
        self.assertEqual(self.store.get_run('parent'),before)
        self.assertFalse(answer['user_supplied_assumptions'][0]['verified'])
        self.assertEqual(self.services.generator.requests[0].purpose,'answer_question')
        self.assertEqual(self.store.get_run(answer['run_id'])['context']['profile'],before['context']['profile'])
        self.assertEqual(answer['qa']['semantic_status'],'not_run')
    def test_wrong_company_or_role_denied_before_generation(self):
        for scope in ({'company_id':'ALIEN'},{'role':'ceo'}):
            with self.assertRaises(PermissionError):self.ask(**scope)
        self.assertEqual(self.services.generator.requests,[])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM agent_runs').fetchone()[0],1)
    def test_withdrawn_and_updated_current_event_stop(self):
        self.current.bundle['event']['availability']='withdrawn'
        answer=self.ask();self.assertIsNone(answer['answer']);self.assertIn('event_withdrawn_recalculation_required',answer['limitations'])
        self.current.bundle['event']['availability']='active';self.current.event_version=2
        answer=self.ask();self.assertIsNone(answer['answer']);self.assertIn('event_updated_recalculation_required',answer['limitations'])
        self.assertEqual(self.services.generator.requests,[])
    def test_historical_replay_explicitly_labelled(self):
        self.current.bundle['event']['availability']='withdrawn';self.services.config['historical_replay']=True
        answer=self.ask();self.assertEqual(answer['status'],'limited');self.assertTrue(answer['historical_replay']);self.assertIn('historical_replay',answer['limitations'])
    def test_missing_current_event_stops_unless_explicit_replay(self):
        self.services.event_store=None
        answer=self.ask();self.assertIn('current_event_unavailable',answer['limitations']);self.assertIsNone(answer['answer'])
    def test_actual_extra_search_bounded_pinned_and_persisted(self):
        self.services.config['question_extra_search']=True
        from llm.rag.corpus import Corpus
        self.services.corpus=Corpus(Path(self.temp.name)/'missing.jsonl')
        def retrieve(bundle,profile,relevance,**kwargs):
            self.calls.append((bundle,profile,relevance,kwargs))
            from llm.rag.retriever import retrieve_evidence
            return retrieve_evidence(bundle,profile,relevance,**kwargs)
        self.services.retriever=retrieve
        answer=self.ask();self.assertTrue(answer['did_extra_search']);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][3]['attempt'],1)
        self.assertEqual(self.calls[0][3]['as_of'].isoformat(),'2026-10-20T15:00:00+00:00')
        child=self.store.get_run(answer['run_id']);self.assertTrue(any(a['kind']=='evidence_retrieved' for a in child['artifacts']))
        self.assertEqual(answer['status'],'limited')
    def test_qa_failure_does_not_publish_answer(self):
        self.services.generator=FixtureGenerator(['foreign_source']);answer=self.ask()
        self.assertIsNone(answer['answer']);self.assertEqual(answer['claims'],[]);self.assertIn('qa_blocked',answer['limitations'])
        qa=[a['payload'] for a in self.store.get_run(answer['run_id'])['artifacts'] if a['kind']=='qa_checked'][0]
        self.assertEqual(qa['publication_status'],'block')
    def test_question_overflow_stops_before_generation_or_retrieval(self):
        from ranking.contracts import canonical_json
        context=deepcopy(self.context)
        context['config']['max_context_bytes']=len(canonical_json(context['payload']).encode())+1
        self.store.create_run('tight',context)
        answer=answer_question('tight','Вопрос больше одного байта',company_id='DEMO_TRADE',role='cfo',services=self.services)
        self.assertIn('context_overflow',answer['limitations'])
        self.assertFalse(answer['did_extra_search'])
        self.assertEqual(self.services.generator.requests,[])
    def test_malformed_retrieval_is_persisted_but_not_used(self):
        from llm.rag.corpus import Corpus
        self.services.config['question_extra_search']=True
        self.services.corpus=Corpus(Path(self.temp.name)/'missing')
        self.services.retriever=lambda *a,**kw: {'hits':[{'quote':'fabricated'}]}
        answer=self.ask()
        self.assertTrue(answer['did_extra_search'])
        self.assertIsNone(answer['answer'])
        self.assertEqual(self.services.generator.requests,[])
        self.assertTrue(any(a['kind']=='evidence_retrieved' for a in self.store.get_run(answer['run_id'])['artifacts']))
    def test_no_default_live_or_bool_semantic_bypass(self):
        self.services.config={};answer=self.ask();self.assertIsNone(answer['answer'])

if __name__=='__main__':unittest.main()
