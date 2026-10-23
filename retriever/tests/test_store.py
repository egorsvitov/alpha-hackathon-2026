import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest

from llm.cluster_merge import core
from llm.cluster_merge.models import Answer, Article, Disposition, Draft, MergeRequest, MergeResult, Paragraph
from retriever.models import ArticleRevision, ClusterSnapshot, EncodedItem, SearchItem, VectorRecord, text_hash
from retriever.store import SQLiteSearchStore


def fixtures(n=3):
    articles = [ArticleRevision(revision_id=f'r{i}', article_id=f'a{i}', text=f'Body {i}.', title=f'Title {i}', source=f'Source {i}') for i in range(n)]
    items = [SearchItem(item_id=a.revision_id, title=a.title, text=a.text, embedding_text=a.text, source_revision_ids=[a.revision_id]) for a in articles]
    vectors = [VectorRecord(vector_id=i.item_id + ':v', item_id=i.item_id, embedding_text_sha256=text_hash(i.embedding_text), values=[1., 0.]) for i in items]
    cluster = ClusterSnapshot(cluster_id='c', revision='1', member_revision_ids=['r0', 'r1'])
    return articles, items, vectors, cluster


def merge(articles, cluster, split=False, unchanged=False):
    request = MergeRequest(cluster_id=cluster.cluster_id, revision=cluster.revision, trigger_id=cluster.member_revision_ids[0], articles=[Article(id=a.revision_id,title=a.title,text=a.text,source=a.source or None) for a in articles if a.revision_id in cluster.member_revision_ids])
    plan = core.plan_merge(request)
    refs = [f.id for f in plan.fragments]
    drafts = [Draft(title='Event', paragraphs=[Paragraph(text='Combined.', fragment_ids=refs)])]
    if split:
        drafts.append(Draft(title='Second event', paragraphs=[Paragraph(text='Second.', fragment_ids=refs)]))
    coverage = [Disposition(fragment_id=f.id, kind='used') for f in plan.fragments]
    answer = Answer(events=drafts, coverage=coverage)
    events = [core.event_from(d, answer, plan.fragments, request, plan.operation_id) for d in drafts]
    result = MergeResult(id='',operation_id=plan.operation_id,input_hash=plan.input_hash,configuration=core.configuration(request),status='completed',mode='unchanged' if unchanged else 'split' if split else 'merged',events=events,coverage=coverage,unresolved=[],calls=1,usage=[{}],expected_cluster_id=cluster.cluster_id,expected_revision=cluster.revision)
    result.id = core.digest(result.model_dump(exclude={'id','cache_hit'}))
    encoded = []
    if not unchanged:
        for e in events:
            item = SearchItem(item_id=e.id,kind='event',title=e.title,text=e.text,embedding_text=e.embedding_text,source_revision_ids=e.article_ids,was_merged=e.was_merged,publication_count=e.publication_count,source_count=e.source_count,derived_from=dict(operation_id=result.operation_id,result_id=result.id,cluster_id=cluster.cluster_id,cluster_revision=cluster.revision,configuration_fingerprint=core.digest(result.configuration),event=e.model_dump()))
            encoded.append(EncodedItem(item=item,vectors=[VectorRecord(vector_id=e.id+':v',item_id=e.id,embedding_text_sha256=text_hash(e.embedding_text),values=[0.,1.])]))
    return request, result, encoded


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'store.sqlite'
        self.store = SQLiteSearchStore(self.path)
        self.a, self.i, self.v, self.c = fixtures()
        self.bundle = dict(articles=self.a,items=self.i,vectors=self.v,clusters=[self.c],encoder_fingerprint='fp',dimension=2,corpus_id='corpus')
        self.store.import_bundle(**self.bundle)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def apply(self, **kwargs):
        req,res,enc = merge(self.a,self.c,**kwargs)
        return self.store.apply_merge(self.c,res,enc,request=req), req,res,enc

    def test_generation_reads_only_metadata_and_tracks_commits(self):
        from unittest.mock import patch
        self.assertEqual(self.store.generation(),1)
        with patch.object(VectorRecord,'model_validate_json',side_effect=AssertionError('vectors must not load')):
            self.assertEqual(self.store.generation(),1)
        self.assertFalse(self.store.import_bundle(**self.bundle)['changed'])
        self.assertEqual(self.store.generation(),1)
        req,res,enc = merge(self.a,self.c)
        self.store._db.execute("CREATE TRIGGER fail_vector BEFORE INSERT ON vectors BEGIN SELECT RAISE(ABORT, 'failure'); END")
        with self.assertRaises(Exception):
            self.store.apply_merge(self.c,res,enc,request=req)
        self.assertEqual(self.store.generation(),1)
        self.store._db.execute('DROP TRIGGER fail_vector')
        self.assertTrue(self.store.apply_merge(self.c,res,enc,request=req).applied)
        other = SQLiteSearchStore(self.path)
        try:
            self.assertEqual(other.generation(),2)
            other.set_clusters([self.c.model_copy(update={'revision':'2'})])
            self.assertEqual(self.store.generation(),3)
        finally:
            other.close()

    def test_membership_index_updates_and_backfills_older_stores(self):
        self.assertEqual([c.cluster_id for c in self.store.clusters_for_item('r0')],['c'])
        revised = ClusterSnapshot(cluster_id='c',revision='2',member_revision_ids=['r1','r2'])
        self.store.set_clusters([revised])
        self.assertEqual(self.store.clusters_for_item('r0'),[])
        self.assertEqual([c.cluster_id for c in self.store.clusters_for_item('r2')],['c'])
        self.store._db.execute('DROP TABLE cluster_members')
        other = SQLiteSearchStore(self.path)
        try:
            self.assertEqual([c.cluster_id for c in other.clusters_for_item('r2')],['c'])
            self.assertEqual(other.clusters_for_item('r0'),[])
            self.assertEqual(other.generation(),2)
        finally:
            other.close()
        self.store.set_clusters([])
        self.assertEqual(self.store.clusters_for_item('r2'),[])

    def test_import_immutable_idempotent_and_snapshot(self):
        self.assertFalse(self.store.import_bundle(**self.bundle)['changed'])
        self.assertEqual(self.store.snapshot().generation,1)
        altered = self.a[0].model_copy(update={'source':'different'})
        with self.assertRaises(ValueError):
            self.store.import_bundle(**{**self.bundle,'articles':[altered,*self.a[1:]]})
        self.assertEqual(self.store.get_sources(['r0'])[0],self.a[0])
        for changes in [dict(encoder_fingerprint='other'),dict(dimension=3),dict(corpus_id='other')]:
            with self.assertRaises(ValueError):
                self.store.import_bundle(**{**self.bundle,**changes})
        self.assertEqual(self.store.snapshot().generation,1)

    def test_apply_split_shared_sources_and_idempotence_reimport(self):
        applied,req,res,enc = self.apply(split=True)
        self.assertTrue(applied.applied)
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot.items),3)
        self.assertEqual(len([i for i in snapshot.items if i.kind=='event']),2)
        self.assertEqual(len(self.store.get_sources(['r0','r1'])),2)
        self.assertIsNone(self.store.get_item('r0'))
        self.assertIsNotNone(self.store.get_item('r0',include_inactive=True))
        self.assertTrue(self.store.apply_merge(self.c,res,enc,request=req).already_applied)
        self.assertFalse(self.store.import_bundle(**self.bundle)['changed'])
        self.assertEqual(self.store.snapshot(),snapshot)

    def test_revision_change_restores_sources_and_old_receipt_stale(self):
        _,req,res,enc = self.apply()
        updated = self.c.model_copy(update={'revision':'2'})
        self.store.set_clusters([updated])
        self.assertEqual([i.item_id for i in self.store.snapshot().items],['r0','r1','r2'])
        self.assertTrue(self.store.apply_merge(self.c,res,enc,request=req).stale)
        self.assertIsNotNone(self.store.get_item(res.events[0].id,include_inactive=True))
        self.store.set_clusters([self.c])
        self.assertTrue(self.store.apply_merge(self.c,res,enc,request=req).stale)

    def test_new_overlap_invalidates_resolved_and_marks_all(self):
        self.apply()
        other = ClusterSnapshot(cluster_id='d',revision='1',member_revision_ids=['r1','r2'])
        self.store.set_clusters([self.c,other])
        self.assertEqual(self.store.get_cluster_snapshot('c').status,'ambiguous')
        self.assertEqual(self.store.get_cluster_snapshot('d').status,'ambiguous')
        self.assertEqual(len(self.store.clusters_for_item('r1')),2)
        self.assertEqual(len(self.store.snapshot().items),3)

    def test_same_revision_membership_and_provenance_changes_rollback(self):
        before = self.store.snapshot()
        for changed in [dict(member_revision_ids=['r0','r2'],membership_hash=''),dict(provenance={'new':1})]:
            c = ClusterSnapshot.model_validate({**self.c.model_dump(),**changed})
            with self.assertRaises(ValueError):
                self.store.set_clusters([c])
            self.assertEqual(self.store.snapshot(),before)
        with self.assertRaises(ValueError):
            self.store.set_clusters([ClusterSnapshot(cluster_id='missing',revision='1',member_revision_ids=['missing'])])
        self.assertEqual(self.store.snapshot(),before)

    def test_late_invalid_encoded_child_rolls_back(self):
        req,res,enc = merge(self.a,self.c,split=True)
        badvector = enc[1].vectors[0].model_copy(update={'embedding_text_sha256':'bad'})
        bad = enc[1].model_copy(update={'vectors':[badvector]})
        before = self.store.snapshot()
        with self.assertRaises(ValueError):
            self.store.apply_merge(self.c,res,[enc[0],bad],request=req)
        self.assertEqual(self.store.snapshot(),before)
        self.assertEqual(self.store.get_cluster_snapshot('c').status,'pending')

    def test_mutated_vector_revalidated_and_dimension(self):
        req,res,enc = merge(self.a,self.c)
        for values in [[float('nan'),0.], [2.,0.], [1.,0.,0.]]:
            e = enc[0].model_copy(deep=True)
            e.vectors[0].values[:] = values
            with self.assertRaises(ValueError):
                self.store.apply_merge(self.c,res,[e],request=req)
        self.assertEqual(self.store.snapshot().generation,1)

    def test_request_exact_archive_and_encoded_shape(self):
        req,res,enc = merge(self.a,self.c)
        req.articles[0].text = 'Tampered'
        with self.assertRaises(ValueError):
            self.store.apply_merge(self.c,res,enc,request=req)
        req,res,enc = merge(self.a,self.c)
        e = enc[0].model_copy(update={'item':enc[0].item.model_copy(update={'publication_count':99})})
        with self.assertRaises(ValueError):
            self.store.apply_merge(self.c,res,[e],request=req)
        self.assertEqual(self.store.snapshot().generation,1)

    def test_noncompleted_and_invalid_result_no_mutation(self):
        req,res,enc = merge(self.a,self.c)
        for changed in [dict(status='partial'),dict(coverage=[]),dict(configuration={}),dict(events=[]),dict(id='forged')]:
            bad = res.model_copy(update=changed)
            with self.assertRaises(ValueError):
                self.store.apply_merge(self.c,bad,enc,request=req)
        self.assertEqual(self.store.snapshot().generation,1)

    def test_two_instances_apply_one_generation(self):
        other = SQLiteSearchStore(self.path)
        req,res,enc = merge(self.a,self.c)
        try:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                results = list(pool.map(lambda s:s.apply_merge(self.c,res,enc,request=req),[self.store,other]))
            self.assertEqual(sum(r.applied for r in results),1)
            self.assertEqual(sum(r.already_applied for r in results),1)
            self.assertEqual(self.store.snapshot().generation,2)
            self.assertEqual(self.store.snapshot(),other.snapshot())
        finally:
            other.close()

    def test_unchanged_zero_output_keeps_originals_and_is_idempotent(self):
        req,res,_ = merge(self.a,self.c)
        fs = core.plan_merge(req).fragments
        res.events = []
        res.coverage = [Disposition(fragment_id=f.id,kind='service',reason='Administrative boilerplate') for f in fs]
        res.mode = 'unchanged'
        res.id = core.digest(res.model_dump(exclude={'id','cache_hit'}))
        outcome = self.store.apply_merge(self.c,res,[],request=req)
        self.assertTrue(outcome.applied)
        self.assertEqual(outcome.item_ids,['r0','r1'])
        self.assertEqual([i.item_id for i in self.store.snapshot().items],['r0','r1','r2'])
        self.assertTrue(self.store.apply_merge(self.c,res,[],request=req).already_applied)

    def test_unchanged_single_article_keeps_original(self):
        cluster = ClusterSnapshot(cluster_id='single',revision='1',member_revision_ids=['r0'])
        self.store.set_clusters([cluster])
        req,res,enc = merge(self.a,cluster,unchanged=True)
        self.assertTrue(self.store.apply_merge(cluster,res,enc,request=req).applied)
        self.assertEqual([i.item_id for i in self.store.snapshot().items],['r0','r1','r2'])

    def test_sql_failure_after_deactivation_rolls_back(self):
        req,res,enc = merge(self.a,self.c)
        before = self.store.snapshot()
        self.store._db.execute("CREATE TRIGGER fail_vector BEFORE INSERT ON vectors BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END")
        with self.assertRaises(Exception):
            self.store.apply_merge(self.c,res,enc,request=req)
        self.assertEqual(self.store.snapshot(),before)
        self.assertEqual(self.store.get_cluster_snapshot('c').status,'pending')
        self.store._db.execute('DROP TRIGGER fail_vector')
        self.assertTrue(self.store.apply_merge(self.c,res,enc,request=req).applied)

    def test_stale_across_connections(self):
        req,res,enc = merge(self.a,self.c)
        other = SQLiteSearchStore(self.path)
        try:
            other.set_clusters([self.c.model_copy(update={'revision':'next'})])
            before = other.snapshot()
            self.assertTrue(self.store.apply_merge(self.c,res,enc,request=req).stale)
            self.assertEqual(other.snapshot(),before)
        finally:
            other.close()

    def test_source_conflict_after_partial_insert_rolls_back_everything(self):
        extra = ArticleRevision(revision_id='new',article_id='new',text='new')
        altered = self.a[0].model_copy(update={'source':'changed'})
        with self.assertRaises(ValueError):
            self.store.import_bundle(**{**self.bundle,'articles':[extra,altered,*self.a[1:]]})
        with self.assertRaises(ValueError):
            self.store.get_sources(['new'])
        self.assertEqual(self.store.snapshot().generation,1)

    def test_fragment_vectors_collapse_to_one_item_and_hash_validation(self):
        article = ArticleRevision(revision_id='frag',article_id='frag',text='Long source')
        item = SearchItem(item_id='frag',text=article.text,embedding_text=article.text,source_revision_ids=['frag'],derived_from={'fragment_text_hashes':[text_hash('Long'),text_hash('source')], 'fragment_texts':{text_hash('Long'):'Long',text_hash('source'):'source'}})
        vectors = [VectorRecord(vector_id=f'f{k}',item_id='frag',embedding_text_sha256=text_hash(text),values=[1.,0.]) for k,text in enumerate(['Long','source'])]
        self.store.import_bundle(**{**self.bundle,'articles':[article], 'items':[item], 'vectors':vectors})
        self.assertEqual(len([i for i in self.store.snapshot().items if i.item_id=='frag']),1)
        bad = vectors[0].model_copy(update={'vector_id':'bad','embedding_text_sha256':'bad'})
        with self.assertRaises(ValueError):
            self.store.import_bundle(**{**self.bundle,'articles':[article], 'items':[item], 'vectors':[bad]})


if __name__ == '__main__':
    unittest.main()
