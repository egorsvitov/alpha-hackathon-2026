import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ranking.profiles import CompanyRepository, canonical_hash
from ranking.graph import attach_payment_graph
from ranking.contracts import resolve_pointer, validate_contract

FIXTURE=Path(__file__).parent/'fixtures/profiles/companies.csv'

class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.profile=CompanyRepository(FIXTURE,is_synthetic=True).get('DEMO_LOGISTICS')

    def graph(self,rows,known=None,profile=None,config=None):
        path=self.root/'payments.csv';path.write_text('PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\n'+rows.replace('A','DEMO_LOGISTICS'))
        return attach_payment_graph([profile or self.profile],known or {'DEMO_LOGISTICS','B'},path,config={'state_dir':self.root,**(config or {})})

    def test_p06_directions_shares_and_self_loop(self):
        result,stats=self.graph('A;B;30;1\nA;C;70;2.0\nB;A;50;1\nA;A;1000;1\nX;Y;8;1\n')
        p=result[0];g=p['graph'];out={n['company_id']:n for n in g['neighbors'] if n['direction']=='outgoing'}
        self.assertEqual(out['B']['observed_share'],.3);self.assertEqual(out['C']['observed_share'],.7)
        self.assertEqual(g['graph_coverage']['outgoing']['retained_amount_raw'],'100');self.assertEqual(stats['self_loops_excluded'],1)
        self.assertFalse(out['C']['profile_known']);self.assertEqual(g['profile_coverage_by_nodes'],.5)
        self.assertEqual(validate_contract(p,'company-profile'),[])
        for f in p['features'].values():self.assertEqual(resolve_pointer(p,f['path']),f['value'])
        for f in p['features'].values():
            if f['path'].startswith('/graph/'):
                artifact=stats['artifacts'][f['origin']['artifact_id']]
                self.assertEqual(canonical_hash(artifact),f['origin']['artifact_sha256'])
                self.assertTrue((self.root/'artifacts'/(f['origin']['artifact_sha256']+'.json')).exists())

    def test_p07_loaded_empty_unknown_node_and_nonmutation(self):
        original=copy.deepcopy(self.profile)
        result,_=self.graph('X;Y;8;1\nA;A;1;1\n')
        self.assertEqual(self.profile,original);self.assertEqual(original['graph']['status'],'not_loaded')
        self.assertEqual(result[0]['graph']['status'],'loaded');self.assertEqual(result[0]['graph']['neighbors'],[])
        self.assertIsNone(result[0]['graph']['graph_coverage']['incoming']['retained_record_fraction'])

    def test_p09_duplicate_loss_not_strong_share(self):
        result,stats=self.graph('A;B;100;1\nA;B;100;1\nA;C;80;1\nA;D;20;1\n')
        g=result[0]['graph'];c=g['graph_coverage']['outgoing']
        self.assertEqual(c['retained_record_fraction'],.5);self.assertEqual(c['share_quality'],'limited')
        self.assertEqual(c['valid_positive_input_amount_raw'],'300');self.assertEqual(c['retained_positive_amount_fraction'],round(1/3,12))
        self.assertEqual({n['company_id']:n['observed_share'] for n in g['neighbors']},{'C':.8,'D':.2})
        self.assertEqual(stats['ambiguous_pair_rows_excluded'],2)

    def test_invalid_duplicate_never_rescues_pair(self):
        result,_=self.graph('A;B;100;1\nA;B;NaN;1\nA;C;80;1\nA;D;20;1\n')
        c=result[0]['graph']['graph_coverage']['outgoing']
        self.assertEqual(c['excluded_ambiguous_records'],2);self.assertEqual(c['excluded_invalid_records'],0)
        self.assertIsNone(c['retained_positive_amount_fraction']);self.assertEqual(c['share_quality'],'limited')

    def test_invalid_amount_count_and_unassignable_quality(self):
        result,_=self.graph('A;B;30;1\nA;C;Infinity;1\nA;D;20;1.5\nX;;50;1\n')
        c=result[0]['graph']['graph_coverage']['outgoing']
        self.assertEqual(c['excluded_invalid_records'],2);self.assertEqual(c['share_quality'],'limited')
        self.assertIsNone(c['retained_positive_amount_fraction'])
        result,_=self.graph('A;B;30;0\n')
        self.assertEqual(result[0]['graph']['graph_coverage']['outgoing']['share_quality'],'unavailable')

    def test_p08_versions_reattach_graph_knownness_policy(self):
        rows='A;B;30;1\nA;C;70;1\n'
        first,_=self.graph(rows);second,_=self.graph(rows,profile=first[0])
        self.assertEqual(first[0],second[0]);self.assertNotEqual(first[0]['profile_version'],self.profile['profile_version'])
        changed,_=self.graph(rows.replace('70','71'))
        self.assertNotEqual(first[0]['profile_version'],changed[0]['profile_version'])
        known,_=self.graph(rows,known={'DEMO_LOGISTICS','B','C'})
        self.assertNotEqual(first[0]['graph']['graph_version'],known[0]['graph']['graph_version'])
        rule,_=self.graph(rows,config={'policy_revision':'test'})
        self.assertNotEqual(first[0]['graph']['graph_version'],rule[0]['graph']['graph_version'])

    def test_repeat_request_uses_versioned_scan_cache(self):
        rows='A;CACHE_NODE;30;1\n'
        first,_=self.graph(rows)
        with patch('ranking.graph.csv_rows', side_effect=AssertionError('CSV scan repeated')):
            second,_=self.graph(rows)
        self.assertEqual(first,second)
