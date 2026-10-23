from dataclasses import replace
import unittest
import numpy as np
from retriever.channels.dense import search_dense
from retriever.embedding_cache import vector_sha256
from retriever.representation_contracts import *
from retriever.channels.lexical import search_lexical
from retriever.channels.structured import search_structured

class DenseTests(unittest.TestCase):
    def test_exact_event_max_and_stable_ties(self):
        q=np.zeros(384,dtype=np.float32);q[0]=1
        a=q.copy();b=np.zeros(384,dtype=np.float32);b[:2]=(.6,.8)
        class Index:
            fingerprint='fp'
            def iter_blocks(self,n):
                yield np.stack([a,b,a]),[ChunkRef(SnapshotKey('a',1,'h'),'z'),ChunkRef(SnapshotKey('b',1,'h'),'x'),ChunkRef(SnapshotKey('a',1,'h'),'a')]
        view=EncodedView('company','business','content','key','fp',vector_sha256(q),'pv',vector=q)
        queries=CompanyQuerySet('company','pv','snapshot',[view]);hits=search_dense(Index(),queries)
        self.assertEqual([h.snapshot.event_id for h in hits],['a','b']);self.assertEqual(hits[0].chunk_ref.chunk_id,'a')
        self.assertAlmostEqual(hits[1].raw_score,.6)
        with self.assertRaises(EncoderMismatch):search_dense(Index(),replace(queries,queries=[replace(view,encoder_fingerprint='bad')]))
    def test_multiple_versions_error(self):
        q=np.zeros(384,dtype=np.float32);q[0]=1
        class Index:
            fingerprint='fp'
            def iter_blocks(self,n):yield np.stack([q,q]),[ChunkRef(SnapshotKey('a',1,'h'),'x'),ChunkRef(SnapshotKey('a',2,'h2'),'y')]
        view=EncodedView('company','business','c','k','fp',vector_sha256(q),'pv',vector=q)
        with self.assertRaises(CorruptVector):search_dense(Index(),CompanyQuerySet('company','pv','snapshot',[view]))
    def test_lexical_no_overlap(self):
        class Index:documents={'e':'Авиаперевозки и аэропорты'}
        self.assertEqual(search_lexical(Index(),['программирование']),[])
        self.assertEqual(search_lexical(Index(),['АВИАПЕРЕВОЗКИ'])[0]['event_id'],'e')


class StructuredTests(unittest.TestCase):
    def test_conditional_finance_industry_and_verified_identity(self):
        import json
        from pathlib import Path
        from types import SimpleNamespace
        from ranking.profiles import CompanyRepository
        from retriever.tests.helpers import FIXTURE
        p=CompanyRepository(FIXTURE,is_synthetic=True).get('DEMO_LOGISTICS')
        b=json.loads(Path('docs/b6-examples/v0.2/normal.json').read_text())
        b['ranking_features']['industries']=['transport']
        event_id=b['event']['event_id']
        index=SimpleNamespace(as_of='2026-10-22T00:00:00Z',snapshots={event_id:SimpleNamespace(bundle=b,event_version=1,source_mode='fixture')})
        mapping={'resolver_version':'test-v1','entries':[{'news_entity_id':'demo:regulator','company_id':p['company_id'],'status':'synthetic','provenance':'synthetic unit fixture'}]}
        hits=search_structured(index,p,entity_mapping=mapping)
        kinds={x['kind'] for x in hits[0]['basis']}
        self.assertTrue({'conditional_exposure','normalized_industry','synthetic_entity'}<=kinds)
        p['is_synthetic']=False
        hits=search_structured(index,p,entity_mapping=mapping)
        self.assertNotIn('synthetic_entity',{x['kind'] for x in hits[0]['basis']})
