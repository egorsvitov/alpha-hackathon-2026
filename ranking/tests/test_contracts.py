import copy
import json
import unittest
from ranking.contracts import PROJECT_ROOT, validate_contract, canonical_json, payload_hash, parse_timestamp

class ContractTests(unittest.TestCase):
    def fixture(self,name):
        return json.loads((PROJECT_ROOT/f'data/examples/personalization/contracts/{name}.json').read_text())
    def test_contract_examples(self):
        for name in ['company-profile','company-profiles','relevance','ranking-result']:
            with self.subTest(name=name):self.assertEqual(validate_contract(self.fixture(name),name),[])
    def test_closed_properties_dates_origin(self):
        p=self.fixture('company-profile');p['extra']=1;self.assertTrue(validate_contract(p,'company-profile'))
        p=self.fixture('company-profile');p['source_snapshot']['imported_at']='2026-10-20T12:00:00';self.assertTrue(validate_contract(p,'company-profile'))
        p=self.fixture('company-profile');p['features']={'f':{'feature_id':'f','path':'/industry','value':None,'quality':'unknown','origin':{'kind':'unknown'}}};self.assertTrue(validate_contract(p,'company-profile'))
    def test_unscored_vs_zero_and_contributions(self):
        r=self.fixture('relevance');r['score']=0
        self.assertIn('scored_consistency',{x.code for x in validate_contract(r,'relevance')})
        r.update(level='low',raw_total=0,scoring_diagnostics={'groups':[],'age_policy':'decay','score_saturated':False})
        self.assertEqual(validate_contract(r,'relevance'),[])
        r['score']=0.5;self.assertIn('contribution_sum',{x.code for x in validate_contract(r,'relevance')})
    def test_profile_refs(self):
        p=self.fixture('company-profile');p['features']={'f':{'feature_id':'f','path':'/industry','value':None,'quality':'derived','origin':{'kind':'derived','input_feature_ids':['f'],'rule_version':'1','artifact_id':None,'artifact_sha256':None}}}
        self.assertIn('feature_reference_invalid',{x.code for x in validate_contract(p,'company-profile')})
    def test_canonical_and_time(self):
        self.assertEqual(payload_hash({'a':1,'b':'я'}),payload_hash({'b':'я','a':1}))
        self.assertNotEqual(payload_hash([1,2]),payload_hash([2,1]))
        with self.assertRaises(ValueError):canonical_json({'n':float('nan')})
        with self.assertRaises(ValueError):parse_timestamp('2026-10-20T12:00:00')
