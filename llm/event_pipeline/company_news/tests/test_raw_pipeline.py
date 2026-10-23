import copy
from pathlib import Path
import tempfile
import unittest
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from pydantic import ValidationError
from retriever.embedding_cache import VectorCache, exact_vector_key, CachedEncoder
from retriever.raw_search import exact_topk, family_fusion
from retriever.representation_contracts import CorruptVector
from llm.event_pipeline.company_news.contract_fixtures import fixture
from llm.event_pipeline.company_news.contracts import NewsAnalysisInput, text_hash, fingerprint, RawNewsCandidateBatch
from llm.event_pipeline.company_news.experiment import embedding_text, eligible
from llm.event_pipeline.company_news.selection import choose_context, reserve_order
from backend.app.analyst.raw_adapter import prepare, validate_card, validate_personal


class RawPipelineTests(unittest.TestCase):
    def test_exact_text_and_encoder_cache_separation(self):
        self.assertNotEqual(exact_vector_key('a','А  Б'),exact_vector_key('a','А Б'))
        self.assertNotEqual(exact_vector_key('a','А Б'),exact_vector_key('b','А Б'))
        with tempfile.TemporaryDirectory() as tmp:
            c=VectorCache(Path(tmp)/'v.sqlite',dimension=1024);v=np.zeros(1024,np.float32);v[0]=1
            c.put('key',v,metadata={});np.testing.assert_array_equal(c.get('key'),v);c.close()
            c=VectorCache(Path(tmp)/'v.sqlite')
            with self.assertRaises(CorruptVector):c.get('key')
            c.close()

    def test_technical_title_unknown_date_and_future_eligibility(self):
        d=fixture().batch.documents[0].model_dump();d['title_origin']='generated_identifier';d['title']='Telegram id123'
        self.assertEqual(embedding_text(d),d['text'])
        d['reference'].update(published_at=None,publication_precision='unknown')
        self.assertEqual(eligible(d,'2026-10-23T00:00:00Z'),'publication_date_unknown')
        d['reference'].update(published_at='2026-10-23',publication_precision='day')
        self.assertIsNotNone(eligible(d,'2026-10-23T12:00:00+03:00'))

    def test_exact_cosine_blocks_and_ties(self):
        q=np.asarray([[1.,0.],[0.,1.]],np.float32)
        r=exact_topk(q,iter([(['z'],q[:1]),(['a','b'],q)]),top_k=2)
        self.assertEqual([uid for uid,_ in r[0]],['a','z']);self.assertEqual(r[1][0][0],'b')
        with self.assertRaises(ValueError):exact_topk(q,iter([(['a'],np.ones((1,3),np.float32))]))

    def test_fusion_no_repeated_votes_and_caps(self):
        h=dict(document_id='a',family='facts',channel='dense',rank=1,raw_score=.9,query_id='q')
        self.assertEqual(family_fusion([h])[0]['fusion_score'],family_fusion([h,h])[0]['fusion_score'])
        hits=[dict(h,document_id=str(i),rank=i+1) for i in range(260)]
        r=family_fusion(hits);self.assertEqual(r[100]['exclusion_reason'],'pool_cap');self.assertEqual(r[250]['exclusion_reason'],'union_cap')

    def test_conflicting_numbers_and_negations_survive_same_group(self):
        d=fixture().batch.documents[0].model_dump();ds={};ordered=[]
        for i,text in enumerate(('Компания подтвердила рост цены на 10%.','Компания не подтвердила рост цены на 15%.')):
            doc=copy.deepcopy(d);uid=str(i);doc['text']=text;doc['reference'].update(document_id=uid,content_hash=text_hash(text));ds[uid]=doc;ordered.append(dict(document_id=uid))
        selected,_,_=choose_context(ordered,ds,[dict(group_id='same',document_ids=['0','1'])],as_of='2026-10-23T00:00:00Z',grouped=True)
        self.assertEqual(len(selected),2)
        with self.assertRaises(ValueError):choose_context(ordered,ds,[],as_of='2026-10-23T00:00:00Z',max_bytes=0)

    def test_reranker_reserve_preserves_retrieval_miss(self):
        original=[dict(document_id=str(i)) for i in range(6)];reranked=list(reversed(original))
        self.assertNotIn('0',[r['document_id'] for r in reranked[:2]])
        self.assertIn('0',[r['document_id'] for r in reserve_order(reranked,original,1)[:2]])

    def test_cross_company_card_cache_and_profile_invalidation(self):
        a=fixture().model_dump();b=copy.deepcopy(a);old=b['batch']['company_id'];new='other'
        b['batch']['company_id']=new;b['batch']['profile_version']='new-version'
        for q in b['batch']['queries']:
            q.update(company_id=new,profile_version='new-version',anchor_feature_ids=[s.replace(old,new) for s in q['anchor_feature_ids']]);q['query_id']=fingerprint({k:v for k,v in q.items() if k!='query_id'})
        for c in b['batch']['candidates']:
            for h in c['hits']:h['query_id']=b['batch']['queries'][0]['query_id']
        for f in b['company_facts']:f['feature_id']=f['feature_id'].replace(old,new)
        pa=prepare(a,model_profile={'model':'offline'});pb=prepare(b,model_profile={'model':'offline'})
        self.assertEqual(pa['common'][0]['key'],pb['common'][0]['key']);self.assertNotEqual(pa['personal']['key'],pb['personal']['key'])
        b['batch']['queries'][0]['profile_version']='stale'
        with self.assertRaises(ValidationError):prepare(b,model_profile={})

    def test_reference_bounds_and_failure_before_apply(self):
        inp=fixture();p=prepare(inp,model_profile={});d=inp.batch.documents[0];s=inp.batch.selected_context[0].spans[0]
        response=dict(theme='Материалы',claims=[dict(text='Опубликованы условия.',kind='reported_fact',references=[s.model_dump()])],differences=[],limitations=[])
        parsed=validate_card(response,{d.reference.document_id:d},[s]);self.assertEqual(parsed['semantic_support'],'unreviewed')
        response['claims'][0]['references'][0]['end']+=1
        with self.assertRaises(ValueError):validate_card(response,{d.reference.document_id:d},[s])
        personal=dict(decision='include',conclusion='Нужно проверить условия.',status='hypothesis',card_keys=['fake'],feature_ids=[],limitations=[])
        with self.assertRaises(ValueError):validate_personal(personal,p)

    def test_incomplete_analyst_budget_rejected_before_transport(self):
        from llm.event_pipeline.company_news.analysis import live
        value=fixture().model_dump();value['batch']['budget']={}
        with self.assertRaisesRegex(ValueError,'incomplete_context_budget'):prepare(value,model_profile={})
        with self.assertRaisesRegex(ValueError,'finite_max_calls_required'):
            live('missing',journal='missing',shared_store='missing',profile_path='missing',budget_path='missing',max_calls=0)

    def test_cached_encoder_dedup_and_restart(self):
        class Encoder:
            fingerprint='synthetic';fingerprint_metadata={'dimension':2}
            def encode_queries(self,texts):
                return np.asarray([[1.,0.] for _ in texts],np.float32)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'cache.sqlite';a=CachedEncoder(Encoder(),path,dimension=2)
            out=a.encode_queries(['one','one','two']);self.assertEqual(out.shape,(3,2));self.assertEqual(a.metrics['inferred'],2);a.cache.close()
            a=CachedEncoder(Encoder(),path,dimension=2);a.encode_queries(['one','two']);self.assertEqual(a.metrics['inferred'],0);self.assertEqual(a.metrics['hits'],2);a.cache.close()

    def test_overlong_reranker_pair_unscored_cached_without_truncation(self):
        from retriever.raw_reranker import CrossEncoder
        m=object.__new__(CrossEncoder);m.fingerprint='synthetic';m.max_pair_tokens=4;m.batch_size=2
        m.cache=sqlite3.connect(':memory:');m.cache.execute('CREATE TABLE pairs(key TEXT PRIMARY KEY,result TEXT NOT NULL)')
        m.tokenizer=lambda pairs,**kwargs:{'input_ids':[[1]*10 for _ in pairs]}
        m.metrics=dict(cache_hits=0,unscored=0)
        fake=SimpleNamespace(cuda=SimpleNamespace(max_memory_allocated=lambda:0,max_memory_reserved=lambda:0))
        with patch.dict('sys.modules',{'torch':fake}):
            first=m.score_pairs([('query','document')]);second=m.score_pairs([('query','document')])
        self.assertEqual(first[0]['status'],'unscored');self.assertEqual(first,second);self.assertEqual(m.metrics['cache_hits'],1);m.cache.close()


if __name__=='__main__':unittest.main()
