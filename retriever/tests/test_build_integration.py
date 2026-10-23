from contextlib import closing
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import json
from unittest.mock import patch
from ranking.profiles import CompanyRepository, file_hash
from retriever.tests.helpers import FakeEncoder,FIXTURE
from retriever.representation_contracts import BuildConfig,GraphPolicy,QueryPolicy,PaymentDatasetManifest,ResumeMismatch,EncoderMismatch,CorruptVector
from retriever.embedding_cache import VectorCache
from retriever.company_build import build_company_snapshot,CompanyEmbeddingSnapshot
from retriever.company_build_cli import export_snapshot
from retriever.payment_store import PaymentGraphStore
from retriever.company_queries import load_company_queries
from ranking.graph import attach_payment_graph

class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.repo=CompanyRepository(FIXTURE,is_synthetic=True);self.encoder=FakeEncoder()
        self.cache=VectorCache(self.root/'objects.sqlite');self.addCleanup(self.cache.close)
        self.config=BuildConfig(str(self.root/'build'),scope_ids=('DEMO_LOGISTICS','DEMO_IT'),company_chunk_size=1)
    def build(self,**kwargs):return build_company_snapshot(self.repo,encoder=self.encoder,cache=self.cache,config=self.config,**kwargs)
    def test_repeat_export_and_reopen(self):
        report=self.build();count=self.encoder.calls
        self.assertGreater(count,0);again=self.build();self.assertEqual(self.encoder.calls,count)
        self.assertEqual(report.snapshot_id,again.snapshot_id)
        with closing(CompanyEmbeddingSnapshot.open(self.root/'build',cache=self.cache)) as snap:
            export_snapshot(snap,destination=self.root/'export')
            views=snap.views('DEMO_LOGISTICS');self.assertEqual(len(views),3)
        with closing(VectorCache(self.root/'export/objects.sqlite')) as cache:
            with closing(CompanyEmbeddingSnapshot.open(self.root/'export/snapshot',cache=cache)) as exported:
                self.assertTrue(exported.verify().valid)
                self.assertEqual(report.snapshot_id,exported.snapshot_id)
    def test_crash_resume_no_partial_publication(self):
        def fault(stage):
            if stage=='after_object_commit':raise RuntimeError('simulated power loss')
        with self.assertRaises(RuntimeError):self.build(fault_hook=fault)
        self.assertFalse((self.root/'build/CURRENT').exists())
        report=self.build(resume=True)
        self.assertEqual(report.status,'complete')
        self.config=replace(self.config,scope_ids=('DEMO_IT',))
        with self.assertRaises(ResumeMismatch):self.build(resume=True)
    def test_atomic_previous_current(self):
        old=self.build();self.config=replace(self.config,scope_ids=('DEMO_LOGISTICS',))
        def fault(stage):
            if stage=='after_rename_before_current':raise RuntimeError('simulated crash')
        with self.assertRaises(RuntimeError):self.build(fault_hook=fault)
        self.assertEqual((self.root/'build/CURRENT').read_text().strip(),old.snapshot_id)
        self.build(resume=True)
    def test_graph_and_temporal_rules(self):
        path=self.root/'payments.csv';path.write_text('PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\nDEMO_LOGISTICS;DEMO_IT;10;1\nDEMO_IT;DEMO_LOGISTICS;30;2\nDEMO_LOGISTICS;UNKNOWN;90;1\n')
        PaymentGraphStore.build(path,output_path=self.root/'graph.sqlite',metadata=PaymentDatasetManifest(str(path),file_hash(path)))
        with closing(PaymentGraphStore.open(self.root/'graph.sqlite')) as graph:
            self.config=replace(self.config,scope_ids=('DEMO_LOGISTICS',),graph_policy=GraphPolicy(enabled=True))
            result=self.build(graph_store=graph)
            self.assertEqual(result.statistics['dependency_only_companies'],1)
        with closing(CompanyEmbeddingSnapshot.open(self.root/'build',cache=self.cache)) as snap:
            strict=load_company_queries(snap,company_id='DEMO_LOGISTICS',expected_encoder_fingerprint=self.encoder.fingerprint,policy=QueryPolicy(graph_enabled=True),as_of='2026-10-22T00:00:00Z')
            self.assertFalse(any(v.kind.startswith('graph') for v in strict.queries))
            static=load_company_queries(snap,company_id='DEMO_LOGISTICS',expected_encoder_fingerprint=self.encoder.fingerprint,policy=QueryPolicy(graph_enabled=True,temporal_mode='static_context'),as_of='2026-10-22T00:00:00Z')
            graphs=[v for v in static.queries if v.kind.startswith('graph')]
            self.assertEqual(len(graphs),2)
            out=next(v for v in graphs if v.direction=='outgoing');self.assertAlmostEqual(out.coverage.vector_amount_fraction,.1)
            self.assertEqual(out.input_vector_refs[0]['normalized_weight'],1)
            with self.assertRaises(EncoderMismatch):load_company_queries(snap,company_id='DEMO_LOGISTICS',expected_encoder_fingerprint='wrong',policy=QueryPolicy(),as_of='2026-10-22T00:00:00Z')

class PaymentTests(unittest.TestCase):
    def test_cleaning_parity_reverse_index_and_reorder(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);repo=CompanyRepository(FIXTURE,is_synthetic=True)
            rows=['DEMO_LOGISTICS;DEMO_IT;10;1','DEMO_LOGISTICS;DUP;10;1','DEMO_LOGISTICS;DUP;NaN;1','DEMO_LOGISTICS;UNKNOWN;90;2','DEMO_IT;DEMO_LOGISTICS;30;2','DEMO_LOGISTICS;DEMO_LOGISTICS;999;1']
            hashes=[]
            for i,order in enumerate([rows,list(reversed(rows))]):
                path=root/f'p{i}.csv';path.write_text('PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\n'+'\n'.join(order)+'\n')
                PaymentGraphStore.build(path,output_path=root/f'g{i}.sqlite',metadata=PaymentDatasetManifest(str(path),file_hash(path)))
                with closing(PaymentGraphStore.open(root/f'g{i}.sqlite')) as graph:
                    with patch('retriever.payment_store.csv_rows',side_effect=AssertionError('CSV scanned on query')):
                        n=graph.neighborhood('DEMO_LOGISTICS',repo=repo)
                    self.assertEqual(n.coverage['outgoing'].retained_amount_raw,'100')
                    self.assertEqual(n.coverage['outgoing'].cleaning_coverage['excluded_ambiguous_records'],2)
                    self.assertEqual(graph.affected_companies({'DEMO_IT'}),{'DEMO_LOGISTICS'})
                    old,_=attach_payment_graph([repo.get('DEMO_LOGISTICS')],repo.company_ids-repo.conflicts,path,config={'state_dir':root/f'old{i}'})
                    self.assertEqual(old[0]['graph']['graph_coverage']['outgoing']['retained_amount_raw'],n.coverage['outgoing'].retained_amount_raw)
                    hashes.append(n.graph_content_hash)
            self.assertEqual(hashes[0],hashes[1])
    def test_legacy_profile_version_independent_of_batch(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);repo=CompanyRepository(FIXTURE,is_synthetic=True)
            path=root/'p.csv';path.write_text('PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\nDEMO_LOGISTICS;DEMO_IT;10;1\n')
            one,_=attach_payment_graph([repo.get('DEMO_LOGISTICS')],repo.company_ids,path,config={'state_dir':root})
            batch,_=attach_payment_graph([repo.get('DEMO_LOGISTICS'),repo.get('DEMO_IT')],repo.company_ids,path,config={'state_dir':root})
            self.assertEqual(one[0]['profile_version'],batch[0]['profile_version'])
