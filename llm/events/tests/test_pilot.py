"""Offline checks for explicit, non-monetary, hard-capped experiments."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from llm.events import EventStore, StorageError
from llm.events.extraction.models import ProviderProfile
from llm.events.extraction.credits import RequestCounter, extract_one
from llm.events.extraction.provider import ProviderResponse
from llm.events.contracts.models import ArticleInput


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_counter_persists_crashes_and_rejects_second_finish(self):
        counter = RequestCounter(self.path/'quota.sqlite3', 1)
        call_id = counter.reserve({'messages': []})
        with self.assertRaises(StorageError):
            RequestCounter(counter.path, 1).reserve({})
        counter.finish(call_id, None, {'code': 'provider_timeout'}, 1.0)
        self.assertIsNone(counter.summary()[0]['cost_rub'])
        with self.assertRaises(StorageError):
            counter.finish(call_id, {}, None, 2.0)
        with self.assertRaises(StorageError):
            RequestCounter(counter.path, 2)

    def test_concurrent_reservations_obey_one_total_cap(self):
        counter = RequestCounter(self.path/'quota.sqlite3', 1)
        def reserve(_):
            try:return counter.reserve({})
            except StorageError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(r is not None for r in pool.map(reserve, range(2))), 1)

    def test_b3_artifacts_cache_and_provider_rate_limit(self):
        import json
        store = EventStore(self.path/'state.sqlite3')
        article = ArticleInput(article_id='public:a', source='public', title='No economic assertion',
            text='Ordinary descriptive words.', url='https://example.org/a', published_at=None,
            language='en', content_kind='original', is_full_original_text=False)
        sid = store.import_batch(json.dumps([article.model_dump(mode='json')]).encode()).items[0].source_id
        counter = RequestCounter(self.path/'quota.sqlite3', 3)
        profile = ProviderProfile(candidate_format='b3.1')
        calls = []
        def complete(request):
            calls.append(request)
            return ProviderResponse({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
                {'claims': [], 'evidence': [], 'excluded': [], 'limitations': []})}}],
                'usage':{'prompt_tokens':10,'completion_tokens':10}})
        provider = SimpleNamespace(profile=profile, is_live=True, check_credentials=lambda:None, complete=complete)
        record = store.get_article(sid)
        first = extract_one(store, record, profile, counter, provider)
        self.assertEqual(first['status'], 'succeeded')
        self.assertEqual(extract_one(store, record, profile, counter, provider)['status'], 'cached')
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(counter.summary()), 1)
        changed = profile.model_copy(update={'max_tokens': 2048})
        provider.profile = changed
        provider.complete = lambda request: ProviderResponse({'error':{'message':'limited'}},429)
        failed = extract_one(store, record, changed, counter, provider)
        self.assertTrue(failed['stop'])
        self.assertEqual(failed['reason'], 'provider_rate_limited')



if __name__ == '__main__':
    unittest.main()
