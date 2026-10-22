"""The documented entry point and collector boundary, without external files or API."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from llm.events.__main__ import main
from llm.events import EventStore, ProviderProfile, StorageError, build_processing_spec
from llm.events.storage.news_adapter import load_news_corpus


class PipelineCLITests(unittest.TestCase):
    def test_help_delegates_to_each_stage_without_api(self):
        with patch('llm.events.extraction.provider.OpenAIProvider.complete', side_effect=AssertionError('network')):
            with redirect_stdout(StringIO()) as text:
                self.assertEqual(main(['--help']), 0)
            self.assertIn('extract', text.getvalue())
            for command in ('storage', 'extract', 'llm', 'normalize', 'catalog', 'publish', 'import-news', 'demo'):
                with self.subTest(command=command), redirect_stdout(StringIO()) as text:
                    with self.assertRaises(SystemExit) as result:
                        main([command, '--help'])
                    self.assertEqual(result.exception.code, 0)
                self.assertIn('usage:', text.getvalue())

    def test_jsonl_import_keeps_text_versions_and_honest_provenance(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            article = {'article_id':'feed:test', 'source':'feed', 'url':'https://example.org/test',
                       'title':'Тест', 'text':'😀 Выручка составила 10 млн рублей.'}
            path = root/'news.jsonl'
            def save(body):
                path.write_text('\n'.join([json.dumps(body, ensure_ascii=False), '{bad json}',
                                          json.dumps({**body, 'article_id':'empty', 'text':''})]))
            save(article)
            self.assertEqual(len(load_news_corpus(path)), 1)
            def run():
                with redirect_stdout(StringIO()) as text:
                    self.assertEqual(main(['import-news', str(path), '--db', str(root/'state.sqlite3')]), 0)
                return json.loads(text.getvalue())
            run()
            store = EventStore(root/'state.sqlite3')
            old = store.get_current_article(article['article_id'])
            self.assertEqual(old.article.text, article['text'])
            self.assertFalse(old.article.is_full_original_text)
            self.assertEqual(run()['counts']['unchanged'], 1)
            save({**article, 'text':'Выручка уточнена до 11 млн рублей.'})
            run()
            self.assertEqual(store.get_current_article(article['article_id']).article_version, 2)
            self.assertEqual(store.get_article(old.source_id).article.text, article['text'])

    def test_retired_profiles_are_readable_but_cannot_dispatch_old_processors(self):
        self.assertEqual(ProviderProfile().candidate_format, 'b3.1')
        for name in ('compact.v1', 'form.v1', 'rows.v1'):
            profile = ProviderProfile(candidate_format=name)
            self.assertEqual(profile.model_dump()['candidate_format'], name)
            with self.assertRaises(StorageError) as result:
                build_processing_spec(profile)
            self.assertEqual(result.exception.code, 'retired_extraction_format')


if __name__ == '__main__':
    unittest.main()
