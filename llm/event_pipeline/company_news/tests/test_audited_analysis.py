"""Synthetic transport replay of the real raw adapter + existing OneShotRunner."""
from pathlib import Path
import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch
from backend.app.analyst.raw_adapter import prepare
from llm.event_pipeline.company_news.contract_fixtures import fixture
from llm.event_pipeline.company_news.analysis import live
from llm.event_pipeline.company_news.io import save,save_rows,sha
from llm.event_pipeline.company_news.contracts import fingerprint
from llm.event_pipeline.runtime import EventStore,ProviderProfile,ExecutionLimits,ProviderResponse
from llm.events.extraction.credits import RequestCounter


class AuditedRawAnalysisTest(unittest.TestCase):
    def test_finite_shared_cap_and_no_retry_or_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);value=fixture();doc=value.batch.documents[0];ref=doc.reference
            article=dict(article_id=ref.article_id,source=ref.source,url=None,title=doc.title,published_at=ref.published_at,
                language='ru',text=doc.text,content_kind='original',is_full_original_text=True,is_synthetic=True)
            corpus=root/'corpus';save_rows(corpus/'documents.jsonl',[dict(document_id=ref.document_id,article=article)])
            manifest=dict(experiment_id='synthetic-live-replay',inputs=dict(corpus=sha(corpus/'documents.jsonl')))
            save(root/'manifest.json',manifest);(root/'manifest.sha256').write_text(fingerprint(manifest));save(root/'plan-files.json',{})
            profile=ProviderProfile(max_tokens=512,max_input_tokens=10000,max_output_tokens=4096,context_window_tokens=32768,limits_confirmed=True)
            limits=ExecutionLimits(live_enabled=True,tariffs_confirmed=True,input_rub_per_million='1',output_rub_per_million='1',daily_quota_rub='10')
            save(root/'profile.json',profile.model_dump(mode='json'));save(root/'budget.json',limits.model_dump(mode='json'))
            prepared=prepare(value,model_profile=profile.model_dump(mode='json'))
            save_rows(root/'analysis-plans.jsonl',[dict(company_id=value.batch.company_id,variant='G4',input=value.model_dump(),prepared=prepared)])
            save(root/'analyze.stage.json',dict(files={'analysis-plans.jsonl':sha(root/'analysis-plans.jsonl')}));save(root/'select.stage.json',dict(files={}))
            store=root/'shared.sqlite3';EventStore(store)
            journal=root/'counter.sqlite3';RequestCounter(journal,3)
            span=value.batch.selected_context[0].spans[0].model_dump()
            output=dict(theme='Синтетический материал',claims=[dict(text=doc.text,kind='reported_fact',references=[span])],differences=[],limitations=[])
            import json
            response=ProviderResponse(dict(choices=[dict(finish_reason='stop',message=dict(content=json.dumps(output,ensure_ascii=False)))],usage=dict(prompt_tokens=100,completion_tokens=100,total_tokens=200)))
            args=dict(journal=journal,shared_store=store,profile_path=root/'profile.json',budget_path=root/'budget.json',max_calls=1,corpus=corpus)
            with patch('llm.events.extraction.admin_cli.load_cli_env'),patch('llm.event_pipeline.runtime.OpenAIProvider.check_credentials'),patch('llm.event_pipeline.runtime.OpenAIProvider.complete',return_value=response) as transport:
                first=live(root,**args);second=live(root,**args)
                self.assertEqual(transport.call_count,1)
            self.assertEqual(first[0]['status'],'succeeded');self.assertEqual(second[0]['status'],'cached')
            with closing(sqlite3.connect(journal)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM calls').fetchone()[0],1)
            with closing(sqlite3.connect(store)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM llm_calls').fetchone()[0],1)
            self.assertTrue((root/'analyst-cache'/(prepared['common'][0]['key']+'.json')).exists())


if __name__=='__main__':unittest.main()
