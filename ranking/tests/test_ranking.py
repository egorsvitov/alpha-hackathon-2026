"""Scoring, temporal, entity safety and feed behavior (R01–R13)."""
import copy
import unittest
from datetime import timedelta
from ranking.tests.test_signals import ROOT,NOW,profile,set_feature,snapshot,cases
from ranking.engine import rank_events,load_rules,candidate_report
from ranking.contracts import validate_contract
from ranking.signals import pointer,prepare_signals
from ranking.b6 import load_bundles

class RankingTests(unittest.TestCase):
    def rank(self,b=None,p=None,rules=None,mapping=None,role='chief_accountant',top_k=5):
        events=copy.deepcopy(b if isinstance(b,list) else [b or cases()[0][1]])
        for bundle in events:
            bundle['sources'][0]['published_at']=bundle['event']['first_published_at']
            for claim in bundle['claims']:
                if claim['time']['effective_at'] is not None:claim['field_evidence']['/time/effective_at']=claim['evidence_ids']
        return rank_events([snapshot(x) for x in events],p or profile(),role=role,as_of=NOW,rules=rules or load_rules(),entity_mapping=mapping,top_k=top_k)
    def test_formula_refs_caps_roles_and_duplicates(self):
        b=cases()[0][1];p=profile();r=self.rank(b,p,role='cfo');rel=r['ranked'][0]['relevance']
        self.assertFalse(validate_contract(r,'ranking-result'))
        self.assertFalse(validate_contract(rel,'relevance'))
        self.assertAlmostEqual(sum(x['contribution'] for x in rel['reasons']),rel['score'])
        self.assertAlmostEqual(rel['raw_total'],sum(x['weighted_points'] for x in rel['scoring_diagnostics']['groups']))
        groups={g['group']:g for g in rel['scoring_diagnostics']['groups']}
        self.assertEqual(groups['product']['raw_points'],28);self.assertEqual(groups['product']['role_multiplier'],1.2)
        self.assertEqual(groups['macro']['raw_points'],4)
        for reason in rel['reasons']:
            for path in reason['event_paths']:pointer(b,path)
            for fid in reason['feature_ids']:self.assertIn(fid,p['features'])
        n=copy.deepcopy(b)
        for i in range(9):
            c=copy.deepcopy(b['claims'][0]);c['claim_id']+=str(i);c['measurements'][0]['measurement_id']+=str(i);n['claims'].append(c)
        self.assertEqual(rel['score'],self.rank(n,p,role='cfo')['ranked'][0]['relevance']['score'])
        n['claims'].reverse();self.assertEqual(rel['score'],self.rank(n,p,role='cfo')['ranked'][0]['relevance']['score'])
        rules=load_rules();rules['mappings']=__import__('ranking.signals',fromlist=['load_mappings']).load_mappings()
        rules['mappings']['rules'][0]['awards'][0]['points']=45
        capped=self.rank(b,p,rules,role='cfo')['ranked'][0]['relevance']['scoring_diagnostics']['groups']
        product=next(x for x in capped if x['group']=='product');self.assertEqual(product['raw_points'],30);self.assertEqual(product['raw_points']*product['role_multiplier'],36)
    def test_no_freshness_points_and_profile_mutation(self):
        b=cases()[3][1];transport=profile();it=profile();set_feature(it,'/industry','it');set_feature(it,'/okved/code','62.01')
        self.assertTrue(self.rank(b,transport)['ranked']);self.assertFalse(self.rank(b,it)['ranked'])
        self.assertIn('no_candidate_signal_unknown_relevance',self.rank(b,it)['deferred'][0]['reason_codes'])
        p=profile()
        for path in ['/products/loan','/products/leasing','/products/deposit']:set_feature(p,path,None,'unknown')
        self.assertFalse(self.rank(cases()[0][1],p)['ranked'])
    def test_forecast_hold_negated_conflicts(self):
        b=copy.deepcopy(cases()[0][1]);b['claims'][0]['modality']='forecast'
        rel=self.rank(b)['ranked'][0]['relevance'];self.assertTrue(all('forecast' in r['text'] and 'action=hold' in r['text'] for r in rel['reasons']))
        b['claims'][0]['polarity']='negated';self.assertFalse(self.rank(b)['ranked'])
        b=load_bundles(ROOT/'docs/b6-examples/conflict.json')[0];rel=self.rank(b)['ranked'][0]['relevance']
        self.assertEqual(rel['evidence_status'],'limited');self.assertNotIn('magnitude',[r['group'] for r in rel['reasons']])
    def test_temporal_edges(self):
        b=copy.deepcopy(cases()[0][1]);b['event']['first_published_at']=None
        self.assertEqual(self.rank(b)['deferred'][0]['reason_codes'],['publication_date_unknown'])
        b=copy.deepcopy(cases()[0][1]);b['event']['availability']='withdrawn';self.assertEqual(self.rank(b)['excluded'][0]['reason_codes'],['event_withdrawn'])
        b=copy.deepcopy(cases()[0][1]);b['event']['first_published_at']=(NOW+timedelta(days=1)).isoformat();self.assertIn(self.rank(b)['excluded'][0]['reason_codes'][0],['publication_in_future','source_publication_in_future'])
        b=copy.deepcopy(cases()[0][1]);extra=copy.deepcopy(b['sources'][0]);extra['source_id']+='future';extra['article_id']+='future';extra['published_at']=(NOW+timedelta(hours=1)).isoformat();b['sources'].append(extra);self.assertIn('source_publication_in_future',self.rank(b)['excluded'][0]['reason_codes'])
        b=copy.deepcopy(cases()[0][1]);b['event']['first_published_at']=(NOW-timedelta(days=31)).isoformat();c=b['claims'][0];c['time']['effective_at']={'start':'2026-10-25','end':'2026-10-25','text':'25 октября'}
        rel=self.rank(b)['ranked'][0]['relevance'];self.assertEqual(rel['scoring_diagnostics']['age_policy'],'upcoming_floor');self.assertEqual(rel['scoring_diagnostics']['groups'][0]['age_weight'],.5)
        c['time']['effective_at']=None;self.assertTrue(self.rank(b)['deferred'])
    def test_durable_recheck_and_repeal(self):
        b=copy.deepcopy(cases()[6][1]);b['event']['first_published_at']=(NOW-timedelta(days=40)).isoformat();c=b['claims'][0];c['stage']='effective';c['time']['effective_at']={'start':'2026-09-15','end':'2026-09-16','text':'15–16 сентября'}
        rel=self.rank(b)['ranked'][0]['relevance'];self.assertIn('durable_recheck',rel['reason_codes']);self.assertEqual(rel['evidence_status'],'limited');self.assertEqual(rel['scoring_diagnostics']['groups'][0]['age_weight'],.5)
        for key,value in [('stage','repealed'),('modality','forecast'),('polarity','negated')]:
            n=copy.deepcopy(b);n['claims'][0][key]=value;self.assertTrue(self.rank(n)['deferred'])
        n=copy.deepcopy(b);c2=copy.deepcopy(c);c2['claim_id']+='-repeal';c2['measurements'][0]['measurement_id']+='-repeal';c2['stage']='repealed';c2['action']='unknown';n['claims'].append(c2);self.assertTrue(self.rank(n)['deferred'])
        n=copy.deepcopy(b);n['event']['first_published_at']=(NOW-timedelta(days=181)).isoformat();self.assertIn('durable_status_requires_refresh',self.rank(n)['deferred'][0]['reason_codes'])
        n=copy.deepcopy(b);n['conflicts']=[{'conflict_id':'bad-date','description':'test','variants':[{'claim_id':c['claim_id'],'field_path':'/time/effective_at/end'},{'claim_id':c['claim_id'],'field_path':'/stage'}]}];self.assertTrue(self.rank(n)['deferred'])
    def entity_bundle(self):
        b=copy.deepcopy(cases()[0][1]);c=b['claims'][0];c['participants'][0]['role']='subject';return b
    def mapping(self,company,status='verified'):
        return {'resolver_version':'test-1','entries':[{'news_entity_id':'demo:regulator','company_id':company,'status':status,'provenance':'hand checked synthetic test'}]}
    def test_mapping_roles_conflicts_synthetic_and_graph(self):
        b=self.entity_bundle();p=profile();mapping=self.mapping(p['company_id'])
        rel=self.rank(b,p,mapping=mapping)['ranked'][0]['relevance'];self.assertIn('entity',rel['candidate_channels'])
        n=copy.deepcopy(b);n['claims'][0]['participants'][0]['role']='forecast_author';self.assertNotIn('entity',self.rank(n,p,mapping=mapping)['ranked'][0]['relevance']['candidate_channels'])
        n=copy.deepcopy(b);cid=n['claims'][0]['claim_id'];n['conflicts']=[{'conflict_id':'bad-entity','description':'test','variants':[{'claim_id':cid,'field_path':'/participants/0/entity_id'},{'claim_id':cid,'field_path':'/participants/0/name'}]}];self.assertNotIn('entity',self.rank(n,p,mapping=mapping)['ranked'][0]['relevance']['candidate_channels'])
        from ranking.entity_mapping import resolve
        synthetic=self.mapping(p['company_id'],'synthetic')
        self.assertFalse(resolve(synthetic,'demo:regulator',as_of=NOW,source_mode='live',is_synthetic=True))
        self.assertFalse(resolve(synthetic,'demo:regulator',as_of=NOW,source_mode='fixture',is_synthetic=False))
        self.assertTrue(resolve(synthetic,'demo:regulator',as_of=NOW,source_mode='fixture',is_synthetic=True))
        mapping=self.mapping('COUNTERPARTY');p['graph']['status']='loaded';p['graph']['neighbors']=[{'company_id':'COUNTERPARTY','direction':'outgoing','observed_share':.8,'feature_id':p['company_id']+':/graph/neighbors/0'}]
        p['graph']['graph_coverage']={'outgoing':{'share_quality':'clean_observed'}}
        rel=self.rank(b,p,mapping=mapping)['ranked'][0]['relevance'];g=next(g for g in rel['scoring_diagnostics']['groups'] if g['group']=='counterparty');self.assertEqual(g['raw_points'],48)
        p['graph']['graph_coverage']['outgoing']['share_quality']='limited';rel=self.rank(b,p,mapping=mapping)['ranked'][0]['relevance'];g=next(g for g in rel['scoring_diagnostics']['groups'] if g['group']=='counterparty');self.assertEqual(g['raw_points'],5)
        p['graph']['graph_coverage']['outgoing']['share_quality']='unavailable';self.assertNotIn('counterparty',self.rank(b,p,mapping=mapping)['ranked'][0]['relevance']['candidate_channels'])
    def test_stable_feed_diversity_and_saturation(self):
        b=self.entity_bundle();p=profile();mapping=self.mapping(p['company_id']);bundles=[]
        for i in range(4):
            n=copy.deepcopy(b);n['event']['event_id']='sort:'+str(i);bundles.append(n)
        r=self.rank(bundles,p,mapping=mapping);self.assertEqual(len(r['ranked']),4);self.assertEqual(len(r['feed']),2)
        self.assertEqual([r['snapshot']['event_id'] for r in r['ranked']],['sort:0','sort:1','sort:2','sort:3'])
        self.assertEqual(r,self.rank(list(reversed(bundles)),p,mapping=mapping))
        rules=load_rules();rules['high_priority_override']=True;self.assertEqual(len(self.rank(bundles,p,rules,mapping,top_k=3)['feed']),3)
        rules=load_rules();rules['roles']['cfo']['entity']=1.2
        r=self.rank(b,p,rules,mapping,role='cfo')['ranked'][0]['relevance'];self.assertEqual(r['score'],1);self.assertGreater(r['raw_total'],100)
        self.assertAlmostEqual(sum(x['contribution'] for x in r['reasons']),1)
        rules['roles']['cfo']['entity']=1.15;r2=self.rank(b,p,rules,mapping,role='cfo')['ranked'][0]['relevance'];self.assertEqual(r2['score'],1);self.assertLess(r2['raw_total'],r['raw_total'])
    def test_thirty_day_entity_decay_and_future_latest(self):
        b=self.entity_bundle();p=profile()
        for path in ['/products/loan','/products/deposit','/products/leasing']:set_feature(p,path,False)
        b['event']['first_published_at']=(NOW-timedelta(days=30)).isoformat()
        rel=self.rank(b,p,mapping=self.mapping(p['company_id']))['ranked'][0]['relevance'];self.assertEqual(rel['score'],.08125)
        old=snapshot(cases()[0][1]);n=copy.deepcopy(cases()[0][1]);n['event']['event_version']=2;n['event']['updated_at']=(NOW+timedelta(hours=1)).isoformat()
        # Snapshot is received, but producer future update is not selected, same as store.
        from dataclasses import replace
        future=replace(old,event_version=2,bundle=n)
        out=rank_events([future,old],profile(),role='cfo',as_of=NOW,rules=load_rules());self.assertEqual(out['ranked'][0]['snapshot']['event_version'],1)

if __name__=='__main__':unittest.main()
