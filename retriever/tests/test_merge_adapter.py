import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from llm.cluster_merge import MergeService, Profile
from llm.cluster_merge.core import digest, validate_merge_result
from llm.cluster_merge.tests.test_merge import Provider, Quota
from retriever.models import ArticleRevision, ClusterSnapshot
from retriever.merge_adapter import ClusterMergeAdapter


class AdapterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.provider, self.quota = Provider(), Quota()
        self.service = MergeService(self.provider, self.quota, Path(tmp.name) / 'state.db')
        self.adapter = ClusterMergeAdapter(self.service)
        self.articles = [ArticleRevision(revision_id=x, article_id=x, text='News ' + x)
                         for x in ('a', 'b')]
        self.cluster = ClusterSnapshot(cluster_id='c', revision='1', member_revision_ids=['a', 'b'])

    def resolve(self, **kwargs):
        return self.adapter.resolve(self.cluster, self.articles, 'a', **kwargs)

    def test_cold_offline_then_complete_then_cross_scope_cache(self):
        self.assertEqual(self.resolve(max_calls=0).new_calls, 0)
        fresh = self.resolve(max_calls=3)
        self.assertEqual((fresh.status, fresh.new_calls, fresh.result.calls), ('completed', 1, 1))
        other = self.adapter.resolve(self.cluster.model_copy(update={'cluster_id': 'other', 'revision': '2'}),
                                     list(reversed(self.articles)), 'b', max_calls=0)
        self.assertEqual((other.status, other.new_calls, other.result.calls), ('completed', 0, 1))
        self.assertEqual(fresh.operation_id, other.operation_id)
        self.assertEqual(other.result.expected_cluster_id, 'other')
        self.assertTrue(validate_merge_result(other.result, other.request).valid)
        self.assertIs(self.service.limiter, self.quota)

    def test_titles_only_source(self):
        self.articles = [a.model_copy(update={'title': 'fake', 'title_origin': 'generated_identifier'})
                         for a in self.articles]
        result = self.resolve(max_calls=0)
        self.assertEqual([a.title for a in result.request.articles], ['', ''])

    def test_exact_membership(self):
        for articles in (self.articles[:1], self.articles + self.articles[:1]):
            with self.assertRaisesRegex(ValueError, 'membership'):
                self.adapter.resolve(self.cluster, articles, 'a', max_calls=1)
        self.assertEqual(self.provider.calls, 0)

    def test_provider_unknown_counts_once_and_blocks_retry(self):
        self.provider.fail = True
        one = self.resolve(max_calls=3)
        two = self.resolve(max_calls=3)
        self.assertEqual((one.new_calls, two.new_calls, self.provider.calls), (1, 0, 1))
        self.assertEqual((one.status, two.status), ('failed', 'failed'))

    def test_unexpected_service_failure_preserves_reservation_count(self):
        def boom(service, request, *, max_calls):
            service.limiter.reserve('attempt')
            raise OSError('private details')
        with patch.object(MergeService, 'merge_cluster', boom):
            result = self.resolve(max_calls=1)
        self.assertEqual((result.status, result.new_calls, result.reason),
                         ('failed', 1, 'merge_service_failure'))

    def test_deadline_gates_new_calls_but_allows_cache(self):
        result = self.resolve(max_calls=3, deadline_seconds=0)
        self.assertEqual((result.new_calls, result.reason), (0, 'merge_deadline_reached'))
        self.assertEqual(self.resolve(max_calls=3).status, 'completed')
        result = self.resolve(max_calls=0, deadline_seconds=0)
        self.assertEqual((result.status, result.new_calls), ('completed', 0))

    def test_real_concurrency_uses_service_lease_and_separate_counters(self):
        entered, release = threading.Event(), threading.Event()
        original = self.provider.complete
        def blocking(*args):
            entered.set()
            if not release.wait(5):
                raise TimeoutError()
            return original(*args)
        self.provider.complete = blocking
        first = []
        thread = threading.Thread(target=lambda: first.append(self.resolve(max_calls=2)))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            second = self.resolve(max_calls=2)
            self.assertEqual((second.reason, second.new_calls), ('in_progress', 0))
        finally:
            release.set()
            thread.join(5)
        self.assertEqual((first[0].new_calls, self.provider.calls), (1, 1))

    def test_configuration_and_content_separate_cache(self):
        first = self.resolve(max_calls=2)
        other = ClusterMergeAdapter(self.service, Profile(reasoning=True))
        self.assertNotEqual(other.configuration_fingerprint, self.adapter.configuration_fingerprint)
        result = other.resolve(self.cluster, self.articles, 'a', max_calls=0)
        self.assertNotEqual(first.operation_id, result.operation_id)
        self.articles[0] = self.articles[0].model_copy(update={'text': 'Changed'})
        self.assertNotEqual(first.operation_id, self.resolve(max_calls=0).operation_id)

    def test_resumed_calls_report_increment_not_lifetime(self):
        from llm.cluster_merge.core import messages
        self.articles = [a.model_copy(update={'text': ('News ' + a.revision_id + ' ') * 300})
                         for a in self.articles]
        probe = self.resolve(max_calls=0).request
        base = len(json.dumps(messages(probe, []), ensure_ascii=False))
        self.adapter = ClusterMergeAdapter(self.service, Profile(max_input_chars=base + 2000))
        one = self.resolve(max_calls=1)
        two = self.resolve(max_calls=1)
        self.assertEqual((one.new_calls, two.new_calls), (1, 1))
        self.assertEqual((one.result.calls, two.result.calls), (1, 2))

    def test_validator_rejects_forged_structures_even_with_fresh_result_id(self):
        good = self.resolve(max_calls=2)
        event = good.result.events[0]
        for change in (
            {'coverage': good.result.coverage + good.result.coverage[:1]},
            {'coverage': [c.model_copy(update={'kind': 'service', 'reason': 'x'}) for c in good.result.coverage]},
            {'events': [event.model_copy(update={'publication_count': 99})]},
            {'events': [event.model_copy(update={'id': 'fabricated'})]},
            {'events': [event, event]},
            {'configuration': {}}, {'mode': 'split'},
        ):
            bad = good.result.model_copy(update=change)
            bad.id = digest(bad.model_dump(exclude={'id', 'cache_hit'}))
            with self.subTest(change=change):
                self.assertFalse(validate_merge_result(bad, good.request).valid)

    def test_all_service_zero_events_is_valid_unchanged(self):
        def service_only(messages, profile):
            fs = json.loads(messages[1]['content'])['fragments']
            answer = {'events': [], 'coverage': [dict(fragment_id=f['id'], kind='service', reason='boilerplate') for f in fs]}
            return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(answer)}}]}
        self.provider.complete = service_only
        result = self.resolve(max_calls=1)
        self.assertEqual((result.status, result.result.mode, result.result.events), ('completed', 'unchanged', []))


if __name__ == '__main__':
    unittest.main()
