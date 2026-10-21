"""Behavioral mutation pairs for every executable mapping (R03–R06/R14)."""
import copy
import json
import unittest
from pathlib import Path
from datetime import datetime
from ranking.contracts import FixtureClock
from ranking.b6 import load_bundles
from ranking.event_store import EventStore,ingest_bundles
from ranking.signals import load_mappings,validate_mappings,prepare_signals,mapping_candidates,pointer

ROOT=Path(__file__).resolve().parents[2]
NOW=datetime.fromisoformat('2026-10-20T18:00:00+03:00')
CLOCK=FixtureClock('2026-10-20T17:00:00+03:00')

def snapshot(bundle):
    with EventStore(':memory:') as store:
        ingest_bundles([bundle],store=store,clock=CLOCK,source_mode='fixture')
        return store.list_latest(as_of=NOW)[0]

def profile():
    p=copy.deepcopy(json.loads((ROOT/'data/examples/personalization/profiles.json').read_text())['profiles'][0])
    values={'/products/loan':True,'/products/deposit':True,'/products/leasing':True,'/okved/code':'24.10','/industry':'transport','/employees_band':'10–49','/fts_present':True,'/countries':['CN']}
    for path,value in values.items():set_feature(p,path,value)
    return p

def set_feature(p,path,value,quality='synthetic'):
    parts=path[1:].split('/');target=p
    for part in parts[:-1]: target=target[part]
    target[parts[-1]]=value
    key=p['company_id']+':'+path
    p['features'][key]={'feature_id':key,'path':path,'value':value,'quality':quality,'origin':{'kind':'synthetic','fixture_id':'rule-tests','fixture_sha256':'0'*64}}


def cases():
    events=load_bundles(ROOT/'data/examples/personalization/events.jsonl')
    by_id={b['event']['event_id']:b for b in events}
    return [('policy_rate_loan_v1',by_id['demo:rate-hold'],'/products/loan',None),('policy_rate_deposit_v1',by_id['demo:rate-hold'],'/products/deposit',False),('policy_rate_leasing_v1',by_id['demo:rate-hold'],'/products/leasing',None),('fuel_transport_v1',by_id['synthetic:fuel'],'/industry','it'),('wages_it_v1',by_id['synthetic:wages'],'/employees_band',None),('steel_manufacturing_v1',by_id['synthetic:steel'],'/okved/code','240'),('restriction_country_v1',by_id['synthetic:restriction'],'/countries',['US']),('fx_fts_v1',by_id['synthetic:fx'],'/fts_present',False)]

class MappingTests(unittest.TestCase):
    def awards(self,b,p):return mapping_candidates(prepare_signals(snapshot(b)),p)[0]
    def test_eight_rules_and_mutations(self):
        for rid,b,path,negative in cases():
            with self.subTest(rule=rid):
                p=profile()
                if rid=='wages_it_v1':set_feature(p,'/industry','it')
                def matched(value,company=p):return [a for a in self.awards(value,company) if a['rule_id']==rid]
                positive=matched(b);self.assertTrue(positive)
                for a in positive:
                    for ep in a['event_paths']:pointer(b,ep)
                    for fid in a['feature_ids']:self.assertIn(fid,p['features'])
                neg=copy.deepcopy(p);set_feature(neg,path,negative);self.assertFalse(matched(b,neg))
                forecast=copy.deepcopy(b);forecast['claims'][0]['modality']='forecast'
                self.assertEqual([a['points'] for a in positive],[a['points'] for a in matched(forecast)])
                self.assertIn('forecast',matched(forecast)[0]['text'])
                for pol in ['negated','unknown']:
                    n=copy.deepcopy(b);n['claims'][0]['polarity']=pol;self.assertFalse(matched(n))
                for modality in ['opinion','unknown']:
                    n=copy.deepcopy(b);n['claims'][0]['modality']=modality;self.assertFalse(matched(n))
                n=copy.deepcopy(b);n['claims'][0]['object']='diesel_company'
                for m in n['claims'][0]['measurements']:m['metric']='unrecognized_metric'
                self.assertFalse(matched(n)) # Neither title nor source alias rescues selector.
                n=copy.deepcopy(b);cid=n['claims'][0]['claim_id'];n['conflicts']=[{'conflict_id':'selector-conflict','description':'test','variants':[{'claim_id':cid,'field_path':'/object'},{'claim_id':cid,'field_path':'/measurements/0/metric'}]}]
                self.assertFalse(matched(n))
                n=copy.deepcopy(b);n['conflicts']=[{'conflict_id':'value-conflict','description':'test','variants':[{'claim_id':cid,'field_path':'/measurements/0/value'},{'claim_id':cid,'field_path':'/measurements/0/qualifier'}]}]
                self.assertTrue(matched(n));self.assertIn('conflicting_claim_fields_no_numeric_inference',matched(n)[0]['limitations'])
                n=copy.deepcopy(b);c=copy.deepcopy(n['claims'][0]);c['claim_id']+='-duplicate';c['measurements'][0]['measurement_id']+='-duplicate';n['claims'].append(c)
                original={(a['group'],a['points']) for a in positive}
                self.assertEqual(original,{(a['group'],a['points']) for a in matched(n)})
                n['claims'].reverse();self.assertEqual(original,{(a['group'],a['points']) for a in matched(n)})
    def test_geography_and_fts_quality(self):
        b=copy.deepcopy(cases()[3][1]);p=profile();b['claims'][0]['geographies']=['US'];self.assertFalse(self.awards(b,p))
        b['claims'][0]['geographies']=[];b['claims'][0]['field_evidence'].pop('/geographies/0',None);self.assertIn('event_geography_unknown',self.awards(b,p)[0]['limitations'])
        b=cases()[6][1]
        for value,quality in [(False,'synthetic'),(None,'unknown'),(True,'conflicting')]:
            p=profile();set_feature(p,'/fts_present',value,quality);self.assertFalse(self.awards(b,p))
        p=profile();set_feature(p,'/countries',['CN'],'conflicting');self.assertFalse(self.awards(b,p))
    def test_config_rejects_unknown_dsl(self):
        for field,value in [('geography_policy','magic')]:
            m=load_mappings();m['rules'][0][field]=value
            with self.assertRaises(ValueError):validate_mappings(m)
        for mutation in ['path','op']:
            m=load_mappings();m['rules'][0]['company_match'][mutation]='magic'
            with self.assertRaises(ValueError):validate_mappings(m)
        m=load_mappings();m['rules'][0]['awards'][0]['group']='magic'
        with self.assertRaises(ValueError):validate_mappings(m)

if __name__=='__main__':unittest.main()
