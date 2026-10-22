import copy
import json
from pathlib import Path
import tempfile
import unittest
from retriever.evaluation.embeddings.dataset import load_dataset
from retriever.evaluation.embeddings.metrics import evaluate_query
from retriever.evaluation.embeddings.runner import run_variant
from retriever.evaluation.embeddings.report import compare_runs
from retriever.representation_contracts import VariantConfig,content_hash
from ranking.profiles import CompanyRepository,file_hash
from ranking.contracts import payload_hash
from retriever.tests.helpers import FIXTURE

class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.profile=CompanyRepository(FIXTURE,is_synthetic=True).get('DEMO_IT')
        self.bundle=json.loads(Path('docs/b6-examples/v0.2/normal.json').read_text());self.uid=self.bundle['event']['event_id']
        self.judgment=dict(company_id='DEMO_IT',company_context_hash=payload_hash(self.profile),role='cfo',event_id=self.uid,event_version=1,payload_hash=payload_hash(self.bundle),story_group_id='story',label=None,status='not_judged',label_source='human',annotator=None,annotation_version='v1',basis_feature_refs=[],basis_claim_refs=[],assumptions=[],synthetic=False)
        self.manifest=dict(role='cfo',splits={'test':[self.uid]},story_groups={self.uid:'story'},files={})
        self.write('profiles',[self.profile]);self.write('events',[self.bundle]);self.write('queries',[{'company_id':'DEMO_IT'}]);self.write('judgments',[self.judgment])
    def write(self,name,value):
        path=self.root/(name+'.json');path.write_text(json.dumps(value));self.manifest['files'][name]={'path':path.name,'sha256':file_hash(path)}
    def load(self):
        path=self.root/'manifest.json';path.write_text(json.dumps(self.manifest));return load_dataset(path)
    def test_unknown_form_and_checksum(self):
        dataset=self.load();self.assertEqual(dataset.judgments[0]['label'],None)
        (self.root/'judgments.json').write_text('[]')
        with self.assertRaisesRegex(ValueError,'checksum'):load_dataset(self.root/'manifest.json')
    def test_weak_is_not_human_and_reference_error(self):
        weak=dict(self.judgment,status='judged',label=3,label_source='weak',annotator='llm')
        self.assertEqual(evaluate_query([self.uid],[weak])['quality_status'],'not_evaluated')
        self.write('judgments',[dict(self.judgment,payload_hash='bad')])
        with self.assertRaises(ValueError):self.load()
    def test_story_cannot_cross_split(self):
        b=copy.deepcopy(self.bundle);b['event']['event_id']='second'
        self.write('events',[self.bundle,b]);self.manifest['story_groups']['second']='story';self.manifest['splits']['dev']=['second']
        with self.assertRaisesRegex(ValueError,'story leakage'):self.load()
    def test_changed_evaluation_config_cannot_be_compared(self):
        c=VariantConfig('D3').to_dict();r={'variant_id':'D3','dataset_hash':'fixed','config':c,'queries':[],'quality_status':'not_evaluated'}
        bad=copy.deepcopy(r);bad['config']['token_budget']=9000
        with self.assertRaisesRegex(ValueError,'incomparable'):compare_runs([r,bad])
    def test_no_positive_denominator(self):
        m=evaluate_query([self.uid],[dict(self.judgment,status='judged',label=0,annotator='test')])
        self.assertIsNone(m['recall_at_20']);self.assertIsNone(m['ndcg_at_10']);self.assertEqual(m['precision_at_20'],0)
