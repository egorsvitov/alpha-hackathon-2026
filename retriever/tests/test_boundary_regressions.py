"""Public-boundary checks against forged manifests and mutable model instances."""
import json

import pytest

from retriever.importers import corpus_sha256, read_vector_bundle
from retriever.models import (ArticleRevision, CompanyInterestSet, IndexSnapshot,
                              Interest, SearchItem, VectorRecord, text_hash)
from retriever.search import exact_search
from retriever.store import SQLiteSearchStore


def corpus():
    articles = [ArticleRevision(revision_id=k, article_id=k, title='Source title', text='Original body ' + k) for k in ['a', 'b']]
    items = [SearchItem(item_id=a.revision_id, title=a.title, text=a.text, embedding_text=a.text, source_revision_ids=[a.revision_id]) for a in articles]
    vectors = [VectorRecord(vector_id=i.item_id, item_id=i.item_id, embedding_text_sha256=text_hash(i.embedding_text), values=[1., 0.]) for i in items]
    return articles, items, vectors


def manifest(articles, items, vectors):
    return dict(schema_version='retriever-vector-bundle.v1', corpus_id='c', corpus_sha256=corpus_sha256(articles),
                article_sources={a.revision_id:a.text_sha256 for a in articles}, encoder_fingerprint='fp', dimension=2,
                items=[i.model_dump() for i in items], vectors=[v.model_dump() for v in vectors])


def interest_set():
    return CompanyInterestSet(company_id='c',profile_version='1',interest_set_version='1',encoder_fingerprint='fp',dimension=2,
                              interests=[Interest(interest_id='q',text='query',vector=[1.,0.])])


@pytest.mark.parametrize('mutation', ['multiple_sources','body','title','embedding'])
def test_initial_article_representations_cannot_spoof_sources(tmp_path, mutation):
    articles, items, vectors = corpus()
    changes = dict(multiple_sources={'source_revision_ids':['a','b']}, body={'text':'Fabricated facts'},
                   title={'title':'Fabricated title'}, embedding={'embedding_text':'Fabricated embedding'})[mutation]
    items[0] = items[0].model_copy(update=changes)
    vectors[0] = vectors[0].model_copy(update={'embedding_text_sha256':text_hash(items[0].embedding_text)})
    path = tmp_path/'manifest.json'
    path.write_text(json.dumps(manifest(articles,items,vectors)))
    with pytest.raises(ValueError):
        read_vector_bundle(path,articles)
    with SQLiteSearchStore(tmp_path/'store.sqlite') as store:
        with pytest.raises(ValueError):
            store.import_bundle(articles=articles,items=items,vectors=vectors,clusters=[],encoder_fingerprint='fp',dimension=2,corpus_id='c')
        with pytest.raises(ValueError, match='store_not_initialized'):
            store.snapshot()


@pytest.mark.parametrize('values', [[2.,0.],[float('nan'),0.],[float('inf'),0.]])
def test_search_revalidates_mutated_vector_instances(values):
    _,items,vectors = corpus()
    snapshot = IndexSnapshot(corpus_id='c',generation=1,encoder_fingerprint='fp',dimension=2,items=items,vectors=vectors)
    snapshot.vectors[0].values[:] = values
    with pytest.raises(ValueError):
        exact_search(snapshot,interest_set())


@pytest.mark.parametrize('mutation', ['norm','nan','dimension','duplicate'])
def test_search_revalidates_mutated_interest_instances(mutation):
    _,items,vectors = corpus()
    snapshot = IndexSnapshot(corpus_id='c',generation=1,encoder_fingerprint='fp',dimension=2,items=items,vectors=vectors)
    interests = interest_set()
    if mutation == 'duplicate':
        interests.interests.append(interests.interests[0])
    else:
        interests.interests[0].vector[:] = dict(norm=[2.,0.],nan=[float('nan'),0.],dimension=[1.,0.,0.])[mutation]
    with pytest.raises(ValueError):
        exact_search(snapshot,interests)


def test_search_rejects_vector_text_identity_mismatch():
    _,items,vectors = corpus()
    vectors[0] = vectors[0].model_copy(update={'embedding_text_sha256':'wrong'})
    snapshot = IndexSnapshot(corpus_id='c',generation=1,encoder_fingerprint='fp',dimension=2,items=items,vectors=vectors)
    with pytest.raises(ValueError):
        exact_search(snapshot,interest_set())


def test_known_model_creation_rejects_nonfinite_interest():
    for invalid in [float('nan'),float('inf'),-float('inf')]:
        with pytest.raises(ValueError):
            Interest(interest_id='q',text='query',vector=[invalid,0.])


def test_store_rejects_mutated_article_hash_before_initialization(tmp_path):
    articles,items,vectors = corpus()
    altered = articles[0].model_copy(update={'text':'Changed without hash'})
    with SQLiteSearchStore(tmp_path/'store.sqlite') as store:
        with pytest.raises(ValueError, match='text_hash_mismatch'):
            store.import_bundle(articles=[altered,articles[1]],items=items,vectors=vectors,clusters=[],encoder_fingerprint='fp',dimension=2,corpus_id='c')
        with pytest.raises(ValueError, match='store_not_initialized'):
            store.snapshot()


def unchanged_result(articles, cluster, configuration_version='1'):
    from llm.cluster_merge import core
    from llm.cluster_merge.models import Article, Disposition, MergeRequest, MergeResult
    request = MergeRequest(cluster_id=cluster.cluster_id,revision=cluster.revision,trigger_id=cluster.member_revision_ids[0],
                           configuration_version=configuration_version,
                           articles=[Article(id=a.revision_id,title=a.title,text=a.text,source=a.source or None) for a in articles if a.revision_id in cluster.member_revision_ids])
    plan = core.plan_merge(request)
    result = MergeResult(id='',operation_id=plan.operation_id,input_hash=plan.input_hash,configuration=core.configuration(request),
                         status='completed',mode='unchanged',events=[],coverage=[Disposition(fragment_id=f.id,kind='service',reason='Administrative boilerplate') for f in plan.fragments],
                         unresolved=[],calls=0,usage=[],expected_cluster_id=cluster.cluster_id,expected_revision=cluster.revision)
    result.id = core.digest(result.model_dump(exclude={'id','cache_hit'}))
    return request,result


def test_unchanged_receipt_pins_profile_until_explicit_cluster_revision(tmp_path):
    from llm.cluster_merge import core
    from retriever.models import ClusterSnapshot
    articles,items,vectors = corpus()
    cluster = ClusterSnapshot(cluster_id='one',revision='1',member_revision_ids=['a','b'])
    with SQLiteSearchStore(tmp_path/'store.sqlite') as store:
        store.import_bundle(articles=articles,items=items,vectors=vectors,clusters=[cluster],encoder_fingerprint='fp',dimension=2,corpus_id='c')
        request,result = unchanged_result(articles,cluster)
        store.apply_merge(cluster,result,[],request=request)
        store.assert_merge_configuration(core.digest(result.configuration))
        assert store.get_applied_configuration(result.id) == result.configuration
        changed_request,changed_result = unchanged_result(articles,cluster,'2')
        with pytest.raises(ValueError,match='merge_configuration_changed'):
            store.assert_merge_configuration(core.digest(changed_result.configuration))
        with pytest.raises(ValueError,match='merge_configuration_changed'):
            store.apply_merge(cluster,changed_result,[],request=changed_request)
        cluster = cluster.model_copy(update={'revision':'2'})
        store.set_clusters([cluster])
        request,result = unchanged_result(articles,cluster,'2')
        assert store.apply_merge(cluster,result,[],request=request).applied


def test_apply_cannot_mix_profiles_across_disjoint_clusters(tmp_path):
    from retriever.models import ClusterSnapshot
    articles,items,vectors = corpus()
    first = ClusterSnapshot(cluster_id='one',revision='1',member_revision_ids=['a'])
    second = ClusterSnapshot(cluster_id='two',revision='1',member_revision_ids=['b'])
    with SQLiteSearchStore(tmp_path/'store.sqlite') as store:
        store.import_bundle(articles=articles,items=items,vectors=vectors,clusters=[first,second],encoder_fingerprint='fp',dimension=2,corpus_id='c')
        request,result = unchanged_result(articles,first)
        store.apply_merge(first,result,[],request=request)
        before = store.snapshot()
        request,result = unchanged_result(articles,second,'different')
        with pytest.raises(ValueError,match='merge_configuration_changed'):
            store.apply_merge(second,result,[],request=request)
        assert store.snapshot() == before
        assert store.get_cluster_snapshot('two').status == 'pending'
        request,result = unchanged_result(articles,second)
        assert store.apply_merge(second,result,[],request=request).applied


@pytest.mark.parametrize('proof', [None,'wrong_text','wrong_hash'])
def test_public_store_requires_exact_fragment_proof(tmp_path,proof):
    articles,items,vectors = corpus()
    hash_ = text_hash('Original')
    derived = {'fragment_text_hashes':[hash_]}
    if proof == 'wrong_text':
        derived['fragment_texts'] = {hash_:'Unrelated'}
    elif proof == 'wrong_hash':
        derived['fragment_texts'] = {'wrong':'Original'}
    items[0] = items[0].model_copy(update={'derived_from':derived})
    vectors[0] = vectors[0].model_copy(update={'embedding_text_sha256':hash_})
    with SQLiteSearchStore(tmp_path/'store.sqlite') as store:
        with pytest.raises(ValueError,match='fragment'):
            store.import_bundle(articles=articles,items=items,vectors=vectors,clusters=[],encoder_fingerprint='fp',dimension=2,corpus_id='c')


def test_concurrent_disjoint_apply_cannot_race_profile_guard(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from retriever.models import ClusterSnapshot
    articles,items,vectors = corpus()
    first = ClusterSnapshot(cluster_id='one',revision='1',member_revision_ids=['a'])
    second = ClusterSnapshot(cluster_id='two',revision='1',member_revision_ids=['b'])
    path = tmp_path/'store.sqlite'
    with SQLiteSearchStore(path) as left, SQLiteSearchStore(path) as right:
        left.import_bundle(articles=articles,items=items,vectors=vectors,clusters=[first,second],encoder_fingerprint='fp',dimension=2,corpus_id='c')
        def apply(args):
            store,cluster,version = args
            request,result = unchanged_result(articles,cluster,version)
            try:
                return store.apply_merge(cluster,result,[],request=request).applied
            except ValueError as exc:
                assert str(exc) == 'merge_configuration_changed_rebuild_index_explicitly'
                return False
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(apply,[(left,first,'1'),(right,second,'2')]))
        assert sorted(outcomes) == [False,True]
        assert left.snapshot().generation == 2
        assert left.snapshot() == right.snapshot()
