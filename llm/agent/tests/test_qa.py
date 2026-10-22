from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest
from llm.agent.context import RunContext
from llm.agent.qa import validate_draft

FIXTURE=Path(__file__).parent/'fixtures/qa_history/forecast.json'
class QATests(unittest.TestCase):
    def setUp(self):
        f=json.loads(FIXTURE.read_text());self.context=RunContext(**f['context']);self.draft=f['draft']
    def check(self, draft=None, context=None, registry=None):
        c=context or self.context
        return validate_draft(draft or self.draft,c,c.registry if registry is None else registry)
    def blocked(self,code):
        q=self.check();self.assertEqual(q.publication_status,'block');self.assertEqual(q.status,'failed')
        self.assertIn(code,[i['code'] for i in q.issues]);self.assertEqual(q.semantic_status,'not_run')
    def test_fixture_never_semantically_passed(self):
        q=self.check();self.assertEqual(q.structural_status,'passed');self.assertEqual(q.publication_status,'limited');self.assertEqual(q.semantic_status,'not_run')
        self.assertIn('independent_semantic_review',q.checks_not_run)
    def test_forecast_negation_text_drift_blocks(self):
        self.draft['claims'][0]['text']='Ставка гарантированно повысится.';self.blocked('source_text_drift')
    def test_foreign_ref_blocks(self):
        self.draft['claims'][0]['source_ids']=['alien'];self.blocked('unknown_source')
    def test_quote_swap_blocks(self):
        registry=deepcopy(self.context.registry);next(iter(registry.values()))['quote']='Подмена'
        q=self.check(registry=registry);self.assertEqual(q.publication_status,'block');self.assertIn('registry_mutation',[i['code'] for i in q.issues])
    def inference(self,text):
        c=self.draft['claims'][0];c.update(kind='ai_inference',section='what_happened',text=text,feature_ids=['DEMO_TRADE:/products/loan'],modality=None,polarity=None,support_status='conditional',assumptions=['Тип ставки и сумма неизвестны.'])
    def test_unsupported_number_blocks_even_if_elsewhere_in_registry(self):
        self.inference('При условии кредита платеж может вырасти на 900%.');self.blocked('unsupported_number')
    def test_unconditional_unknown_contract_blocks(self):
        self.inference('Платеж по кредиту вырастет.');self.draft['claims'][0]['support_status']='supported';self.draft['claims'][0]['assumptions']=[];self.blocked('unknown_exposure_unconditional')
    def test_false_bank_attribution_blocks(self):
        self.inference('Альфа-Банк считает условия выгодными.');self.blocked('bank_attribution_unreviewed')
    def test_bank_view_metadata_alone_blocks(self):
        c=self.draft['claims'][0];c.update(kind='bank_view',section='bank_view');self.draft['section_states']['what_happened']={'status':'insufficient_evidence','reason':'none'};self.draft['section_states']['bank_view']={'status':'available','reason':None};self.blocked('bank_meaning_unreviewed')
    def test_foreign_company_context_blocks(self):
        c=deepcopy(self.context.to_dict());c['relevance']['company_id']='ALIEN';self.context=RunContext(**c);self.blocked('context_identity_mismatch')
    def test_excluded_source_blocks(self):
        self.context=replace(self.context,excluded_artifacts=list(self.context.registry));self.blocked('excluded_source')
    def test_malformed_typed_draft_blocks_without_crash(self):
        for key,value in [('section_states',[]),('claims',[None]),('unknowns','bad')]:
            draft=deepcopy(self.draft);draft[key]=value
            self.assertEqual(self.check(draft).publication_status,'block')
    def test_number_in_unrelated_source_does_not_authorize_inference(self):
        self.inference('При условии кредита платеж может вырасти на 900%.')
        registry=deepcopy(self.context.registry)
        registry['unrelated']={**next(iter(registry.values())),'quote':'900%'}
        self.context=replace(self.context,registry=registry)
        self.blocked('unsupported_number')
    def test_conflict_number_not_authorized_by_quote_overlap(self):
        from llm.agent.source_registry import build_source_registry
        from llm.agent.generators.fixture import FixtureGenerator
        from llm.agent.ports import GenerationRequest
        c=deepcopy(self.context.to_dict())
        b=json.loads(Path('docs/b6-examples/conflict.json').read_text())
        c['snapshot'].update(bundle=b,event_id=b['event']['event_id'])
        c['relevance']['event_id']=b['event']['event_id']
        c['registry']=build_source_registry(c['snapshot'])
        c['payload'].update(claims=b['claims'],conflicts=b['conflicts'],news_sources=list(c['registry'].values()))
        self.context=RunContext(**c)
        self.draft=FixtureGenerator().generate(GenerationRequest('interpret','r','g','offline-fixture-v1','test',c['payload'],{})).payload
        self.inference('При условии кредита ставка 20% может повлиять на платеж.')
        self.blocked('conflicting_number_inference')
    def test_empty_available_section_blocks(self):
        self.draft['section_states']['risk']={'status':'available','reason':None};self.blocked('section_state_mismatch')

if __name__=='__main__':unittest.main()
