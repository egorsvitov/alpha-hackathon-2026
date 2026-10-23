from contextlib import closing
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import copy
import csv
import numpy as np
from retriever.tests.helpers import FakeEncoder,FIXTURE
from retriever.representation_contracts import *
from retriever.embedding_cache import VectorCache,vector_key,vector_sha256
from retriever.graph_embeddings import aggregate_graph_views
from retriever.company_build import build_company_snapshot,CompanyEmbeddingSnapshot
from retriever.embeddings import masked_mean,Encoder
from ranking.profiles import CompanyRepository

class CacheTests(unittest.TestCase):
    def test_key_validation_collision_corruption_and_lock(self):
        e=FakeEncoder();v=e.encode_queries(['x'])[0]
        self.assertNotEqual(vector_key(e.fingerprint,'x'),vector_key(e.fingerprint,'x',input_kind='document'))
        with tempfile.TemporaryDirectory() as d,closing(VectorCache(Path(d)/'cache')) as c,closing(VectorCache(Path(d)/'cache')) as other:
            c.put('key',v,metadata={})
            with self.assertRaises(VectorCollision):c.put('key',e.encode_queries(['y'])[0],metadata={})
            for bad in (np.zeros(384,dtype=np.float32),np.full(384,np.nan,dtype=np.float32),np.ones(10,dtype=np.float32),v.astype(np.float64)):
                with self.assertRaises(CorruptVector):c.put('bad',bad,metadata={})
            with c.writer_lock():
                with self.assertRaises(BuildLocked):
                    with other.writer_lock():pass
            with c.connection:c.connection.execute("UPDATE objects SET data=? WHERE key='key'",(b'bad',))
            with self.assertRaises(CorruptVector):c.get('key')
    def test_masked_pooling_and_explicit_cuda_refusal(self):
        hidden=np.zeros((1,2,384),dtype=np.float32);hidden[0,0,0]=2;hidden[0,1,1]=9
        result=masked_mean(hidden,[[1,0]])
        self.assertEqual(result[0,0],1.);self.assertEqual(result[0,1],0.)
        with self.assertRaises(EncoderUnavailable):Encoder(EncoderConfig(device_policy='cuda'))

class GraphArithmeticTests(unittest.TestCase):
    def setup_graph(self):
        a=np.zeros(384,dtype=np.float32);a[0]=1;b=np.zeros(384,dtype=np.float32);b[1]=1
        vectors={cid:EncodedView(cid,'business',cid,cid,'encoder',vector_sha256(v),'pv',vector=v) for cid,v in [('a',a),('b',b)]}
        class Lookup:
            def get_business(self,cid):return vectors.get(cid)
        neighbors=[Neighbor('a','incoming','edge-a','25',1,.25,'usable','a','pv'),Neighbor('b','incoming','edge-b','75',1,.75,'usable','b','pv')]
        n=PaymentNeighborhood('target',PaymentDatasetManifest('test','hash'),'g','p',incoming=neighbors)
        return n,Lookup(),vectors
    def test_weight_recipes(self):
        n,lookup,_=self.setup_graph()
        for policy,expected in [('equal',[1.,1.]),('amount_share',[.25,.75]),('sqrt_share',[.5,np.sqrt(.75)])]:
            result=aggregate_graph_views(n,lookup=lookup,policy=GraphPolicy(enabled=True,weight_policy=policy));v=result.views['graph_incoming'].vector
            expected=np.asarray(expected);expected/=np.linalg.norm(expected)
            np.testing.assert_allclose(v[:2],expected,rtol=1e-6)
            self.assertEqual(result.omitted['graph_outgoing'],'no_business_vectors')
    def test_missing_cancel_and_stale_neighbor(self):
        n,lookup,vectors=self.setup_graph();old=vectors['b'];vectors['b']=replace(old,vector=-vectors['a'].vector,vector_sha256=vector_sha256(-vectors['a'].vector))
        result=aggregate_graph_views(n,lookup=lookup,policy=GraphPolicy(enabled=True,weight_policy='equal'))
        self.assertEqual(result.omitted['graph_incoming'],'near_zero_norm')
        vectors['b']=replace(old,profile_version='stale')
        with self.assertRaises(CorruptVector):aggregate_graph_views(n,lookup=lookup,policy=GraphPolicy(enabled=True))

class RecoveryTests(unittest.TestCase):
    def test_all_commit_boundaries(self):
        for stage in ['before_vector_commit','after_object_commit','before_mapping_commit','before_checkpoint','after_checkpoint','before_publish','after_rename_before_current']:
            with self.subTest(stage=stage),tempfile.TemporaryDirectory() as d,closing(VectorCache(Path(d)/'cache')) as cache:
                repo=CompanyRepository(FIXTURE,is_synthetic=True);encoder=FakeEncoder();cfg=BuildConfig(str(Path(d)/'build'),scope_ids=('DEMO_IT','DEMO_LOGISTICS'),company_chunk_size=1)
                def fail(s):
                    if s==stage:raise RuntimeError(stage)
                with self.assertRaises(RuntimeError):build_company_snapshot(repo,encoder=encoder,cache=cache,config=cfg,fault_hook=fail)
                result=build_company_snapshot(repo,encoder=encoder,cache=cache,config=cfg,resume=True)
                with closing(CompanyEmbeddingSnapshot.open(result.snapshot_path,cache=cache)) as snapshot:self.assertTrue(snapshot.verify().valid)
    def test_edit_one_product_reuses_business_and_removes_finance(self):
        with tempfile.TemporaryDirectory() as d,closing(VectorCache(Path(d)/'cache')) as cache:
            repo=CompanyRepository(FIXTURE,is_synthetic=True);encoder=FakeEncoder();cfg=BuildConfig(str(Path(d)/'build'),scope_ids=('DEMO_LOGISTICS',))
            first=build_company_snapshot(repo,encoder=encoder,cache=cache,config=cfg)
            with closing(CompanyEmbeddingSnapshot.open(first.snapshot_path,cache=cache)) as snapshot:before={v.kind:v.vector_key for v in snapshot.views('DEMO_LOGISTICS')}
            text=FIXTURE.read_text();dialect=';' if ';' in text.splitlines()[0] else ','
            with FIXTURE.open() as f:reader=csv.DictReader(f,delimiter=dialect);fields=reader.fieldnames;rows=list(reader)
            from ranking.profiles import PRODUCT_FIELDS
            for row in rows:
                if row['SYNTHETIC_U']=='DEMO_LOGISTICS':
                    for col in PRODUCT_FIELDS.values():row[col]='0'
            edited=Path(d)/'companies.csv'
            with edited.open('w') as f:
                writer=csv.DictWriter(f,fieldnames=fields,delimiter=dialect);writer.writeheader();writer.writerows(rows)
            changed=CompanyRepository(edited,is_synthetic=True);second=build_company_snapshot(changed,encoder=encoder,cache=cache,config=cfg)
            self.assertEqual(second.statistics['encoded_texts'],0)
            with closing(CompanyEmbeddingSnapshot.open(second.snapshot_path,cache=cache)) as snapshot:
                after={v.kind:v.vector_key for v in snapshot.views('DEMO_LOGISTICS')}
                self.assertNotIn('finance',after);self.assertEqual(before['business'],after['business'])
