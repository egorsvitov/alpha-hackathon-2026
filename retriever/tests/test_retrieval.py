import copy
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace
from ranking.event_store import EventStore
from ranking.profiles import CompanyRepository
from retriever.tests.helpers import FakeEncoder,FIXTURE
from retriever.index import build_event_index
from retriever.representation_contracts import BuildConfig,QueryPolicy,VariantConfig,CompanyQuerySet
from retriever.embedding_cache import VectorCache
from retriever.company_build import build_company_snapshot,CompanyEmbeddingSnapshot
from retriever.company_queries import load_company_queries
from retriever.service import RetrievalService
from retriever.contracts import validate_candidate_batch
from retriever.fusion import fuse
from retriever.evaluation.embeddings.metrics import evaluate_query
import json

CORPUS=Path('docs/b6-examples/v0.2/normal.json')
class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.repo=CompanyRepository(FIXTURE,is_synthetic=True);self.profile=self.repo.get('DEMO_LOGISTICS')
        self.encoder=FakeEncoder();self.cache=VectorCache(self.root/'cache');self.addCleanup(self.cache.close)
        build_company_snapshot(self.repo,encoder=self.encoder,cache=self.cache,config=BuildConfig(str(self.root/'company'),scope_ids=('DEMO_LOGISTICS',)))
        self.snapshot=CompanyEmbeddingSnapshot.open(self.root/'company',cache=self.cache);self.addCleanup(self.snapshot.close)
        self.store=EventStore(self.root/'events');self.addCleanup(self.store.close)
        # Actual input is used only as a regression fixture, never a human label.
        self.bundle=json.loads(CORPUS.read_text())
        self.store.put(self.bundle,received_at='2026-10-21T22:24:12Z',source_mode='fixture')
        self.times=dict(as_of='2026-10-22T00:00:00Z',window_start='2026-10-20T00:00:00Z',window_end='2026-10-22T00:00:00Z')
    def index(self):return build_event_index(self.store,encoder=self.encoder,output_path=self.root/'index',cache=self.cache,**self.times)
    def test_end_to_end_original_preserved_and_refs_checked(self):
        index=self.index();queries=load_company_queries(self.snapshot,company_id='DEMO_LOGISTICS',expected_encoder_fingerprint=index.fingerprint,policy=QueryPolicy(),as_of=self.times['as_of'])
        request=dict(request_id='test',company_id='DEMO_LOGISTICS',profile=self.profile,role='cfo',**self.times)
        result=RetrievalService(index).run(request,company_queries=queries,config=VariantConfig('H3',**self.times,token_budget=30000))
        batch=result['candidate_batch'];self.assertEqual(batch['status'],'ready')
        self.assertEqual(batch['candidates'][0]['members'][0]['bundle'],self.bundle)
        bad=copy.deepcopy(batch);bad['candidates'][0]['members'][0]['bundle']['claims'][0]['text']='forged'
        with self.assertRaises(ValueError):validate_candidate_batch(bad,profile=self.profile,index=index)
        bad=copy.deepcopy(batch);bad['candidates'][0]['match_context']['feature_ids']=['FOREIGN:/industry']
        with self.assertRaises(ValueError):validate_candidate_batch(bad,profile=self.profile,index=index)
        tight=RetrievalService(index).run(request,company_queries=queries,config=VariantConfig('H3',**self.times,token_budget=1))
        self.assertEqual(tight['candidate_batch']['empty_reason'],'budget_exhausted')
    def test_long_claim_windows_keep_negation_and_tail(self):
        from types import SimpleNamespace
        from retriever.event_views import build_event_views
        b=copy.deepcopy(self.bundle)
        b['claims'][0]['text']='Не подтверждено. ' + 'длинный контекст '*600 + 'Финальное важное утверждение.'
        b['claims'][0]['polarity']='negated'
        stored=SimpleNamespace(bundle=b,event_id=b['event']['event_id'],event_version=1,payload_sha256='synthetic-unit-only')
        _,texts,refs=build_event_views(stored,encoder=self.encoder)
        claim_refs=[r for r in refs if r.claim_refs]
        self.assertGreater(len(claim_refs),1)
        covered={i for r in claim_refs for offset in r.offsets for i in range(offset['start'],offset['end'])}
        self.assertEqual(covered,set(range(len(b['claims'][0]['text']))))
        self.assertIn('Финальное важное утверждение.',texts[-1])
        for text,ref in zip(texts,refs):
            self.assertLessEqual(self.encoder.token_lengths([text],input_kind='document')[0],512)
            if ref.claim_refs:self.assertIn('negated',text)

    def test_latest_withdrawn_does_not_resurrect_old(self):
        b=copy.deepcopy(self.bundle);b['event']['event_version']=2;b['event']['availability']='withdrawn'
        self.store.put(b,received_at='2026-10-21T23:00:00Z',source_mode='fixture')
        index=self.index();self.assertEqual(index.snapshots,{})
    def test_future_receipt_and_encoder_fingerprint(self):
        times=dict(self.times,as_of='2026-10-21T22:24:10Z',window_end='2026-10-21T22:24:10Z')
        index=build_event_index(self.store,encoder=self.encoder,output_path=self.root/'early',**times)
        self.assertEqual(index.snapshots,{})
    def test_chunk_coverage(self):
        index=self.index();text=self.bundle['claims'][0]['text'];covered=set()
        for chunk in index.chunks:
            for offset in chunk.offsets:
                if offset['path']=='/claims/0/text':covered.update(range(offset['start'],offset['end']))
        self.assertEqual(covered,set(range(len(text))))

class MetricsTests(unittest.TestCase):
    def test_hand_calculation_unknowns_duplicates(self):
        judgments=[dict(event_id=str(i),label=2 if i<5 else 0,status='judged',label_source='human') for i in range(7)]
        m=evaluate_query(['0','1','5','6','0'],judgments)
        self.assertEqual(m['recall_at_20'],.4);self.assertEqual(m['precision_at_20'],.1);self.assertEqual(m['precision_returned'],.5)
        judgments[0].update(label=None,status='not_judged');m=evaluate_query(['0','1'],judgments)
        self.assertIsNone(m['recall_at_20']);self.assertEqual(m['quality_status'],'partial')
    def test_dense_views_max_not_sum(self):
        from types import SimpleNamespace
        hits=[SimpleNamespace(to_dict=lambda r=r:{'snapshot':{'event_id':'x'},'channel':'dense','rank':r}) for r in (1,2,3,4,5)]
        result=fuse(hits,[],[],config=VariantConfig('H3'))
        self.assertAlmostEqual(result[0]['score'],1/61)
