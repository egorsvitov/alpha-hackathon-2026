"""Source preparation, provenance and preliminary search without external data or API."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from llm.events import ArticleInput, EventStore, StorageError
from llm.events.corpus.prepare import flatten_text, publication, prepare_corpus
from llm.events.corpus.neighbors import candidate_neighborhoods, load_corpus


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.raw=self.root/'raw';self.raw.mkdir();self.output=self.root/'prepared'

    def telegram(self):
        return {'name':'Вымышленный канал','type':'public_channel','id':123,
                'messages':[{'id':7,'type':'message','date':'2026-10-18T03:00:00',
                             'date_unixtime':'1792281600','text':['😀 ',{'type':'bold','text':'Рост\u00a0спроса'},'\n10%.']},
                            {'id':8,'type':'message','text':'','date_unixtime':'1792281600'},
                            {'id':9,'type':'service','action':'channel_created'}]}

    def collector(self, **changes):
        return {'article_id':'source:1','source':'source','url':None,'title':'Выпуск завода',
                'text':'Завод увеличил выпуск продукции.','language':'ru','published_at':'2026-10-18',**changes}

    def write(self,telegram=None, rows=None):
        (self.raw/'1.json').write_text(json.dumps(telegram or self.telegram(),ensure_ascii=False))
        (self.raw/'news_corpus.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in (rows or [self.collector()])))

    def test_rich_text_unicode_coordinates_and_unsupported_parts(self):
        text,spans=flatten_text(['😀 ',{'text':'Банк\u00a0России'},'\r\nСтавка'], '/messages/0/text')
        self.assertEqual(text,'😀 Банк\u00a0России\r\nСтавка')
        self.assertEqual(text[spans[1]['start']:spans[1]['end']],'Банк\u00a0России')
        self.assertEqual(spans[1]['pointer'],'/messages/0/text/1/text')
        with self.assertRaises(ValueError):flatten_text([{'type':'unknown'}],'/text')

    def test_publication_preserves_precision_and_trusts_epoch(self):
        stamp,meta=publication('2026-10-18T03:00:00','1792281600')
        self.assertEqual(stamp,'2026-10-18T00:00:00+00:00')
        self.assertEqual(meta['basis'],'unix_timestamp')
        self.assertIsNone(publication('2026-10-18')[0])
        self.assertEqual(publication('2026-10-18')[1]['precision'],'day')
        self.assertIsNone(publication('2026-10-18T03:00:00')[0])
        self.assertEqual(publication('2026-10-18T03:00:00+03:00')[0],'2026-10-18T03:00:00+03:00')
        for raw in ['2026-02-30','nonsense']:
            with self.assertRaises(ValueError):publication(raw)
        with self.assertRaises(ValueError):publication(None,'bad')

    def test_prepare_source_integrity_idempotence_and_b2(self):
        self.write();before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.raw.iterdir()}
        prepared=prepare_corpus(self.raw,self.output)
        self.assertEqual(prepared['audit']['documents'],2)
        self.assertEqual(prepared['audit']['excluded_or_invalid'],2)
        manifest,docs=load_corpus(self.output)
        telegram=next(d for d in docs if d['article']['article_id'].startswith('telegram:'))
        self.assertEqual(telegram['article']['article_id'],'telegram:123:7')
        self.assertEqual(telegram['article']['text'],'😀 Рост\u00a0спроса\n10%.')
        self.assertFalse(telegram['article']['is_full_original_text'])
        self.assertEqual(telegram['article']['language'],'und')
        self.assertIsNone(telegram['article']['url'])
        self.assertEqual(prepare_corpus(self.raw,self.output)['status'],'cached')
        self.assertEqual(before,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.raw.iterdir()})
        store=EventStore(self.root/'state.sqlite3')
        self.assertEqual(store.import_batch(self.output/'articles.json').counts.new,2)
        self.assertEqual(store.import_batch(self.output/'articles.json').counts.unchanged,2)
        for d in docs:ArticleInput.model_validate(d['article'])
        (self.output/'documents.jsonl').write_text('corrupt')
        with self.assertRaises(StorageError):prepare_corpus(self.raw,self.output)

    def test_unknown_export_and_malformed_jsonl_remain_visible(self):
        self.write(telegram={**self.telegram(),'type':'personal_chat'})
        with (self.raw/'news_corpus.jsonl').open('a') as f:f.write('{broken}\n')
        result=prepare_corpus(self.raw,self.output)
        rejected=json.loads((self.output/'rejected.json').read_text())
        self.assertEqual({x['code'] for x in rejected},{'unsupported_telegram_export','invalid_json'})
        self.assertEqual(result['audit']['documents'],1)

    def test_repeated_text_not_collapsed_and_revisions_not_ambiguous_batch(self):
        row=self.collector();self.write(rows=[row,{**row,'article_id':'source:2'},
                                            {**row,'text':'Завод сообщил другие сведения.'}])
        result=prepare_corpus(self.raw,self.output)
        self.assertEqual(result['audit']['exact_text_duplicate_excess'],1)
        self.assertEqual(len(result['audit']['multiple_versions']['source:1']),2)
        self.assertFalse(result['manifest']['b2_batch_available'])
        self.assertFalse((self.output/'articles.json').exists())
        self.assertEqual(len(load_corpus(self.output)[1]),4)
        (self.raw/'extra.jsonl').write_text(json.dumps(self.collector(article_id='extra:1'))+'\n')
        with self.assertRaises(StorageError) as caught:prepare_corpus(self.raw,self.output)
        self.assertEqual(caught.exception.code,'corpus_changed')

    def test_candidate_neighborhoods_are_cross_source_dated_and_unassigned(self):
        docs=[]
        for i in range(20):
            article=ArticleInput.model_validate({**self.collector(article_id=f's:{i}',source=f'source-{i}',
                published_at='2026-10-18T00:00:00+00:00',text=f'Редкийтермин{i} предложение\nОбщая навигация сайта и подписка'),
                'content_kind':'original','is_full_original_text':False})
            docs.append({'document_id':str(i),'article':article.model_dump(),
                         'provenance':[{'title_origin':'source','publication':{'precision':'instant'}}]})
        for i in (0,1):docs[i]['article']['text']='Северный завод повысил выпуск продукции.\nОбщая навигация сайта и подписка'
        original=deepcopy(docs)
        result=candidate_neighborhoods(docs,top_k=1,max_groups=2,per_source=1,window_days=0)
        self.assertTrue(result['groups'])
        group=result['groups'][0]
        self.assertEqual(group['document_ids'],['0','1'])
        self.assertEqual(group['identity_status'],'unassessed')
        self.assertEqual(group['split'],'unassigned')
        self.assertGreater(result['retrieval_view']['suppressed_common_line_occurrences'],0)
        self.assertEqual(docs,original)
        docs[1]['article']['published_at']='2026-09-01T00:00:00+00:00'
        self.assertFalse(candidate_neighborhoods(docs,window_days=0)['groups'])


if __name__=='__main__':unittest.main()
