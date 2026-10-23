"""Offline acceptance of the complete retrieval -> merge -> apply path."""
import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from llm.cluster_merge import MergeService, Profile
from retriever.merge_adapter import ClusterMergeAdapter
from retriever.models import (ArticleRevision, ClusterSnapshot, CompanyInterestSet,
                              Interest, RetrievalRequest, SearchItem, VectorRecord,
                              canonical, text_hash)
from retriever.selection import RetrieverService
from retriever.store import SQLiteSearchStore


class IntegrationQuota:
    def __init__(self):
        self.reservations = []

    def reserve(self, key):
        if key in self.reservations:
            raise AssertionError('attempt reserved twice')
        self.reservations.append(key)


class IntegrationProvider:
    def __init__(self, mode='merged'):
        self.mode = mode
        self.calls = 0

    def complete(self, messages, profile):
        self.calls += 1
        if self.mode == 'failure':
            raise TimeoutError('uncertain transport')
        fs = json.loads(messages[1]['content'])['fragments']
        refs = [f['id'] for f in fs]
        events = [] if self.mode == 'unchanged' else [
            dict(title='weak', paragraphs=[dict(text='Merged material', fragment_ids=refs)])]
        if self.mode == 'split':
            # One source may support both events. They must remain two outputs.
            events.append(dict(title='strong', paragraphs=[dict(text='Separate material', fragment_ids=refs)]))
        coverage = [dict(fragment_id=f['id'], kind='service', reason='boilerplate')
                    if self.mode == 'unchanged' else dict(fragment_id=f['id'], kind='used') for f in fs]
        answer = dict(events=events, coverage=coverage)
        return dict(choices=[dict(finish_reason='stop', message=dict(content=json.dumps(answer)))])


class IntegrationEncoder:
    fingerprint = 'test-encoder'
    dimension = 2

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def encode_event_texts(self, texts):
        self.calls.append(list(texts))
        values = np.array([[1., 0.] if t.startswith('strong\n') else [0., 1.] for t in texts], dtype=np.float32)
        if self.fail:
            values[-1] = [float('nan'), 0.]
        return values


class SelectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SQLiteSearchStore(Path(self.tmp.name) / 'search.db')
        self.addCleanup(self.store.close)
        self.provider, self.quota = IntegrationProvider(), IntegrationQuota()
        self.merge = ClusterMergeAdapter(MergeService(self.provider, self.quota, Path(self.tmp.name) / 'merge.db'))
        self.encoder = IntegrationEncoder()
        self.interests = CompanyInterestSet(company_id='company', profile_version='1', interest_set_version='1',
            encoder_fingerprint='test-encoder', dimension=2,
            interests=[Interest(interest_id='focus', text='Focus', vector=[1., 0.])])
        self.request = RetrievalRequest(request_id='query', company_id='company', profile_version='1', corpus_id='corpus',
            as_of='2026-10-24T12:00:00Z', k=2, max_new_llm_calls=10)

    def load(self, scores=None, groups=None, dates=None, texts=None):
        scores = scores or {'a': 1., 'b': .9, 'c': .8}
        groups = [['a', 'b']] if groups is None else groups
        dates = dates or {}
        articles = [ArticleRevision(revision_id=id_, article_id=id_, text=(texts or {}).get(id_, 'Source ' + id_),
                        **dates.get(id_, {})) for id_ in scores]
        items = [SearchItem(item_id=a.revision_id, text=a.text, embedding_text=a.text,
                            source_revision_ids=[a.revision_id]) for a in articles]
        vectors = [VectorRecord(vector_id=i.item_id + ':v', item_id=i.item_id,
                    embedding_text_sha256=text_hash(i.embedding_text),
                    values=[scores[i.item_id], math.sqrt(1-scores[i.item_id] ** 2)]) for i in items]
        clusters = [ClusterSnapshot(cluster_id=f'cluster-{n}', revision='1', member_revision_ids=sorted(g))
                    for n, g in enumerate(groups)]
        self.store.import_bundle(articles=articles, items=items, vectors=vectors, clusters=clusters,
            encoder_fingerprint='test-encoder', dimension=2, corpus_id='corpus')

    def run_query(self, *, request=None, encoder=None, **changes):
        request = request or self.request.model_copy(update=changes)
        return RetrieverService().retrieve(request, interests=self.interests, store=self.store,
            merge=self.merge, encoder=self.encoder if encoder is None else encoder)

    def test_merged_inherits_trigger_then_next_company_query_uses_new_cosine(self):
        self.load()
        first = self.run_query()
        merged, plain = first.batch.candidates
        self.assertEqual((merged.item.kind, plain.item.item_id), ('event', 'c'))
        self.assertEqual((merged.score_origin, merged.selection_score, merged.embedding_score),
                         ('inherited_trigger', 1., 0.))
        self.assertEqual(set(merged.item.source_revision_ids), {'a', 'b'})
        self.assertEqual({a.revision_id for a in first.batch.sources}, {'a', 'b', 'c'})
        self.interests = self.interests.model_copy(update={'company_id': 'second-company'})
        second = self.run_query(company_id='second-company', max_new_llm_calls=0)
        self.assertEqual(second.batch.candidates[0].item.item_id, 'c')
        self.assertEqual(second.batch.candidates[1].score_origin, 'cosine')
        self.assertEqual((second.trace['calls_new'], self.provider.calls), (0, 1))

    def test_split_outputs_rerank_and_shared_sources_are_preserved(self):
        self.load()
        self.provider.mode = 'split'
        result = self.run_query()
        self.assertEqual([c.item.title or c.item.item_id for c in result.batch.candidates], ['strong', 'c'])
        self.assertTrue(all(c.score_origin == 'cosine' for c in result.batch.candidates))
        events = [i for i in self.store.snapshot().items if i.kind == 'event']
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].source_revision_ids, events[1].source_revision_ids)
        self.assertEqual(len(self.encoder.calls[0]), 2)

    def test_cluster_below_top_k_has_no_paid_work(self):
        self.load(scores={'a': .5, 'b': .4, 'c': 1.})
        result = self.run_query(k=1)
        self.assertEqual(result.batch.candidates[0].item.item_id, 'c')
        self.assertEqual((self.provider.calls, result.trace['calls_new']), (0, 0))

    def test_unknown_failure_never_retries_and_next_cluster_has_no_budget(self):
        self.load(scores={'a': 1., 'b': .9, 'c': .8, 'd': .7}, groups=[['a', 'b'], ['c', 'd']])
        self.provider.mode = 'failure'
        before = self.store.snapshot()
        first = self.run_query(k=4, max_new_llm_calls=1)
        self.assertEqual((first.trace['calls_new'], self.provider.calls), (1, 1))
        self.assertEqual(len(first.batch.deferred), 2)
        self.assertEqual(first.batch.status, 'partial')
        self.assertEqual(self.store.snapshot(), before)
        second = self.run_query(k=4, max_new_llm_calls=0)
        self.assertEqual((second.trace['calls_new'], self.provider.calls), (0, 1))

    def test_all_children_encoder_failure_rolls_back_and_reuses_complete_cache(self):
        self.load()
        self.provider.mode = 'split'
        self.encoder.fail = True
        before = self.store.snapshot()
        first = self.run_query()
        self.assertEqual(first.batch.status, 'partial')
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.store.get_cluster_snapshot('cluster-0').status, 'pending')
        calls = self.provider.calls
        self.encoder.fail = False
        second = self.run_query(max_new_llm_calls=0)
        self.assertEqual(second.batch.status, 'ready')
        self.assertEqual((second.trace['calls_new'], self.provider.calls), (0, calls))
        self.assertEqual(len([i for i in self.store.snapshot().items if i.kind == 'event']), 2)

    def test_unchanged_marks_revision_resolved_without_encoding_or_repeat(self):
        self.load()
        self.provider.mode = 'unchanged'
        first = self.run_query(k=3)
        self.assertEqual([c.item.item_id for c in first.batch.candidates], ['a', 'b', 'c'])
        self.assertEqual(self.store.get_cluster_snapshot('cluster-0').status, 'resolved')
        second = self.run_query(k=3, max_new_llm_calls=0)
        self.assertEqual((self.provider.calls, second.trace['calls_new'], len(self.encoder.calls)), (1, 0, 0))

    def test_overlap_strict_and_explicit_raw_fallback(self):
        self.load(groups=[['a', 'b'], ['b', 'c']])
        strict = self.run_query(k=3)
        self.assertEqual((strict.batch.status, strict.batch.candidates), ('partial', []))
        fallback = self.run_query(k=3, raw_fallback=True)
        self.assertEqual(len(fallback.batch.candidates), 3)
        self.assertTrue(all('raw_fallback_may_include_duplicates' in c.limitations for c in fallback.batch.candidates))
        self.assertEqual(self.provider.calls, 0)

    def test_future_member_defers_full_cluster_and_does_not_send_subset(self):
        self.load(dates={'b': dict(published_at='2026-10-25T00:00:00Z', publication_precision='timestamp')})
        result = self.run_query()
        self.assertEqual([c.item.item_id for c in result.batch.candidates], ['c'])
        self.assertEqual(result.batch.deferred[0]['reason'], 'cluster_has_unavailable_sources')
        self.assertEqual(self.provider.calls, 0)

    def test_outside_window_old_member_is_still_merged_as_full_cluster(self):
        self.load(dates={'a': dict(published_at='2026-10-24', publication_precision='day'),
                         'b': dict(published_at='2026-09-01', publication_precision='day')})
        result = self.run_query(window_start='2026-10-23T00:00:00Z')
        event = next(c.item for c in result.batch.candidates if c.item.kind == 'event')
        self.assertEqual(event.source_revision_ids, ['a', 'b'])

    def test_partial_context_envelope_is_bounded_and_tiny_envelope_rejected(self):
        self.load()
        baseline = self.run_query(max_new_llm_calls=0)
        minimum = len(canonical(baseline.batch.model_copy(update={'candidates': [], 'sources': []})).encode()) + 100
        result = self.run_query(max_new_llm_calls=0, max_context_bytes=minimum)
        self.assertEqual(result.batch.status, 'partial')
        self.assertLessEqual(len(canonical(result.batch).encode()), minimum)
        with self.assertRaisesRegex(ValueError, 'metadata_envelope'):
            self.run_query(max_context_bytes=1)

    def test_partial_merge_resumes_with_incremental_budget_and_no_partial_application(self):
        self.load(texts={'a': 'Source a ' * 500, 'b': 'Source b ' * 500})
        from llm.cluster_merge.core import canonical as merge_canonical, messages, plan_merge
        probe = self.merge.resolve(self.store.get_cluster_snapshot('cluster-0'),
            self.store.get_sources(['a', 'b']), 'a', max_calls=0).request
        singleton_limit = max(len(merge_canonical(messages(probe, [f])))
                              for f in plan_merge(probe).fragments) + 10
        profile = Profile(max_input_chars=singleton_limit)
        planned = plan_merge(probe.model_copy(update={'profile': profile}))
        self.assertGreaterEqual(len(planned.batches), 3)
        self.merge = ClusterMergeAdapter(self.merge.service, profile)
        before = self.store.snapshot()
        partial = self.run_query(max_new_llm_calls=1)
        self.assertEqual((partial.trace['calls_new'], partial.batch.status), (1, 'partial'))
        self.assertEqual(self.store.snapshot(), before)
        complete = self.run_query(max_new_llm_calls=1)
        self.assertEqual((complete.trace['calls_new'], complete.batch.status), (1, 'partial'))
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.provider.calls, 2)

    def test_revision_changes_during_paid_response_cas_retries_from_cache(self):
        self.load()
        original = self.provider.complete
        changed = []
        def updating(messages, profile):
            if not changed:
                cluster = self.store.get_cluster_snapshot('cluster-0')
                self.store.set_clusters([cluster.model_copy(update={'revision': '2'})])
                changed.append(True)
            return original(messages, profile)
        self.provider.complete = updating
        result = self.run_query()
        self.assertEqual((result.batch.status, result.trace['calls_new'], self.provider.calls), ('ready', 1, 1))
        self.assertEqual(len(result.trace['operations']), 2)
        event = next(c.item for c in result.batch.candidates if c.item.kind == 'event')
        self.assertEqual(event.derived_from['cluster_revision'], '2')
        self.assertEqual(self.store.get_cluster_snapshot('cluster-0').status, 'resolved')

    def test_operation_limit_returns_partial_without_api(self):
        self.load()
        result = self.run_query(max_merge_operations=0)
        self.assertEqual((result.batch.status, self.provider.calls), ('partial', 0))
        self.assertEqual(result.batch.deferred[0]['reason'], 'merge_operation_limit')

    def test_unchanged_resolved_cluster_rejects_changed_merge_profile(self):
        self.load()
        self.provider.mode = 'unchanged'
        self.run_query()
        self.assertEqual(self.store.get_cluster_snapshot('cluster-0').status, 'resolved')
        self.merge = ClusterMergeAdapter(self.merge.service, Profile(reasoning=True))
        with self.assertRaisesRegex(ValueError, 'configuration'):
            self.run_query()
        self.assertEqual(self.provider.calls, 1)

    def test_deadline_after_provider_response_preserves_paid_cache_without_apply(self):
        self.load()
        before = self.store.snapshot()
        now = [0.]
        original = self.provider.complete
        def advancing(messages, profile):
            response = original(messages, profile)
            now[0] = 2.
            return response
        self.provider.complete = advancing
        result = RetrieverService(clock=lambda: now[0]).retrieve(
            self.request.model_copy(update={'deadline_seconds': 1.}), interests=self.interests,
            store=self.store, merge=self.merge, encoder=self.encoder)
        self.assertEqual((result.batch.status, result.trace['calls_new'], self.provider.calls), ('partial', 1, 1))
        self.assertEqual(result.batch.deferred[0]['reason'], 'deadline_after_merge_response')
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.encoder.calls, [])
        retried = self.run_query(max_new_llm_calls=0)
        self.assertEqual((retried.batch.status, retried.trace['calls_new'], self.provider.calls), ('ready', 0, 1))

    def test_deadline_after_encoding_does_not_apply_and_cache_retry_is_free(self):
        self.load()
        before = self.store.snapshot()
        now = [0.]
        original = self.encoder.encode_event_texts
        def advancing(texts):
            result = original(texts)
            now[0] = 2.
            return result
        self.encoder.encode_event_texts = advancing
        result = RetrieverService(clock=lambda: now[0]).retrieve(
            self.request.model_copy(update={'deadline_seconds': 1.}), interests=self.interests,
            store=self.store, merge=self.merge, encoder=self.encoder)
        self.assertEqual((result.batch.status, result.trace['calls_new'], self.provider.calls), ('partial', 1, 1))
        self.assertIn('TimeoutError', result.batch.deferred[0]['reason'])
        self.assertEqual(self.store.snapshot(), before)
        self.assertEqual(self.store.get_cluster_snapshot('cluster-0').status, 'pending')
        retried = self.run_query(max_new_llm_calls=0)
        self.assertEqual((retried.batch.status, retried.trace['calls_new'], self.provider.calls), ('ready', 0, 1))

    def test_deadline_prevents_new_calls(self):
        self.load()
        ticks = iter([0., 2., 3., 4.])
        result = RetrieverService(clock=lambda: next(ticks)).retrieve(
            self.request.model_copy(update={'deadline_seconds': 1.}), interests=self.interests,
            store=self.store, merge=self.merge, encoder=self.encoder)
        self.assertEqual((result.batch.stop_reason, result.batch.status, self.provider.calls), ('deadline', 'partial', 0))


if __name__ == '__main__':
    unittest.main()


def test_incomplete_group_with_one_known_member_is_not_a_singleton(tmp_path):
    from retriever.tests.test_search_import import interests
    article=ArticleRevision(revision_id='a',article_id='a',text='News')
    item=SearchItem(item_id='a',text='News',embedding_text='News',source_revision_ids=['a'])
    vector=VectorRecord(vector_id='a',item_id='a',embedding_text_sha256=text_hash('News'),values=[1.,0.])
    cluster=ClusterSnapshot(cluster_id='partial',revision='1',member_revision_ids=['a'],
        provenance={'incomplete_membership':True,'missing_revision_ids':['missing']})
    profile=interests([Interest(interest_id='q',text='q',vector=[1.,0.])])
    request=RetrievalRequest(request_id='q',company_id='company',profile_version='p1',corpus_id='c',as_of='2026-10-24T00:00:00Z',k=1)
    with SQLiteSearchStore(tmp_path/'partial.sqlite3') as store:
        store.import_bundle(articles=[article],items=[item],vectors=[vector],clusters=[cluster],encoder_fingerprint='fp',dimension=2,corpus_id='c')
        run=RetrieverService().retrieve(request,interests=profile,store=store)
        assert not run.batch.candidates
        assert run.batch.deferred[0]['reason']=='ambiguous_membership'
