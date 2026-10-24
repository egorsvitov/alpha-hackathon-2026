"""Offline product acceptance: real importer/retriever/merge/adapter/analyst/API."""
import json
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.full_cycle.storage import Workspace,PipelineError
from app.full_cycle.synthetic import setup,services,run,AS_OF,analyst_draft
from app.full_cycle.ingest import ingest,atomic_json
from app.full_cycle.jobs import run_job
from app.full_cycle.api import SavedDataStore
from app.full_cycle.runtime import analyst_runtime,QuotaLLM
from app.analyst.llm_gateway import FakeLLM,BudgetExceeded,LLMUnavailable
from retriever.models import digest
from retriever.store import SQLiteSearchStore


@pytest.fixture(autouse=True)
def no_network():
    with patch('httpx.HTTPTransport.handle_request',side_effect=AssertionError('offline test attempted HTTP')), \
         patch('requests.sessions.Session.request',side_effect=AssertionError('offline test attempted requests')):
        yield


@pytest.fixture
def fixture(tmp_path):
    ws=Workspace(tmp_path)
    corpus,profiles=setup(ws)
    encoder,provider,merge,analyst,fake=services(ws)
    return SimpleNamespace(ws=ws,corpus=corpus,profiles=profiles,encoder=encoder,provider=provider,merge=merge,analyst=analyst,fake=fake)


def job(f,**kw):
    return run_job(f.ws,f.profiles,'SYNTHETIC_FACTORY','cfo',AS_OF,analyst=f.analyst,
                   encoder=f.encoder,merge=f.merge,k=10,max_merge_calls=10,**kw)


def test_file_to_http_split_merge_restart_and_no_repeated_work(fixture):
    f=fixture
    first=ingest(f.ws,f.corpus,encoder=f.encoder)
    assert (first['article_count'],first['group_count'])==(4,2)
    output=job(f)
    assert output['status']=='ready',output.get('error_code')
    assert output['counts']=={'include':3}
    saved=SavedDataStore(f.ws.root)
    feed=saved.get_feed('SYNTHETIC_FACTORY','cfo')
    assert len(feed)==3
    assert len({i.event.canonical_event_id for i in feed})==3
    groups=[set(i.event.reading_group_ids) for i in feed]
    assert any(groups[i]&groups[j] for i in range(3) for j in range(i))
    assert sorted(len(i.event.revision_ids) for i in feed)==[1,1,2]
    assert any(i.event.date is None and i.event.publication_precision=='unknown' for i in feed)
    assert {o['status'] for o in output['outcomes']}=={'include'}
    before=(len(f.fake.requests),f.provider.calls,f.encoder.calls)
    again=ingest(f.ws,f.corpus,encoder=f.encoder)
    assert not again['changed']
    assert job(f)['reused']
    assert before==(len(f.fake.requests),f.provider.calls,f.encoder.calls)
    # A new Workspace and runtime simulate process restart, not just reused objects.
    ws=Workspace(f.ws.root)
    enc,provider,merge,analyst,fake=services(ws)
    assert run_job(ws,f.profiles,'SYNTHETIC_FACTORY','cfo',AS_OF,analyst=analyst,encoder=enc,merge=merge,k=10,max_merge_calls=10)['reused']
    assert (len(fake.requests),provider.calls,enc.calls)==(0,0,0)
    from backend.main import app
    import app.api.v1.endpoints as endpoints
    with patch.dict('os.environ',{'FULL_CYCLE_MODE':'persisted'}),patch.object(endpoints,'store',SavedDataStore(ws.root)):
        with TestClient(app) as client:
            companies=client.get('/api/v1/companies').json()
            assert {c['company_id'] for c in companies}=={'SYNTHETIC_FACTORY','SYNTHETIC_LOGISTICS'}
            response=client.get('/api/v1/companies/SYNTHETIC_FACTORY/news-feed?role=cfo')
            assert response.status_code==200,response.text
            http_feed=response.json()
            assert {i['event']['event_id'] for i in http_feed}=={i.event.event_id for i in feed}
            for item in http_feed:
                event=item['event']
                response=client.get('/api/v1/news/'+event['event_id']+'/analysis?company_id=SYNTHETIC_FACTORY&role=cfo')
                assert response.status_code==200,response.text
                insight=response.json()
                assert insight['event']['candidate_id']==event['candidate_id']
                assert insight['event']['canonical_event_id']==event['canonical_event_id']
                assert insight['event']['derived_text_origin']=='derived_merge'
                by_revision={s.revision_id:s for s in SQLiteSearchStore(first['path']).get_sources(event['revision_ids'])}
                for e in insight['evidence']:
                    if not e['revision_id']: continue
                    original=by_revision[e['revision_id']]
                    assert e['url']==original.url
                    assert e['quote']==getattr(original,e['field'])[e['start']:e['end']]
                assert client.get('/api/v1/news/'+event['event_id']+'/analysis?company_id=SYNTHETIC_LOGISTICS&role=cfo').status_code==404
            state=client.get('/api/v1/companies/SYNTHETIC_FACTORY/processing-state?role=cfo').json()
            assert state['status']=='ready'
            assert client.get('/api/v1/companies/DOES_NOT_EXIST/news-feed').status_code==404


def test_ingestion_failures_keep_last_index(fixture):
    f=fixture
    first=ingest(f.ws,f.corpus,encoder=f.encoder)
    old=f.ws.state('index')
    with pytest.raises(PipelineError,match='encoder_profile_mismatch'):
        ingest(f.ws,f.corpus,encoder=f.encoder,expected_fingerprint='wrong')
    assert f.ws.state('index')==old
    assert f.ws.state('ingestion')['status']=='failed'
    f.corpus.write_text('')
    with pytest.raises(PipelineError,match='empty_corpus'): ingest(f.ws,f.corpus,encoder=f.encoder)
    assert f.ws.state('index')==old
    assert len(SQLiteSearchStore(first['path']).snapshot().items)==4


def test_immutable_revision_conflict_across_restarts(fixture):
    f=fixture
    rows=[json.loads(x) for x in f.corpus.read_text().splitlines()]
    for i,row in enumerate(rows):row['revision_id']='fixed-'+str(i)
    f.corpus.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    ingest(f.ws,f.corpus,encoder=f.encoder)
    before=f.ws.state('index');calls=f.encoder.calls
    rows[0]['text']+=' amended'
    f.corpus.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    with pytest.raises(PipelineError,match='immutable_revisions_conflict'):
        ingest(Workspace(f.ws.root),f.corpus,encoder=f.encoder)
    assert f.encoder.calls==calls
    assert f.ws.state('index')==before


def test_correction_creates_new_revision_and_archives_old(fixture):
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    rows=[json.loads(x) for x in f.corpus.read_text().splitlines()]
    rows[0]['text']+=' correction'
    f.corpus.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    result=ingest(f.ws,f.corpus,encoder=f.encoder)
    assert result['new_articles']==1
    with f.ws.db() as db:assert db.execute('SELECT count(*) FROM revisions').fetchone()[0]==5
    assert len(SQLiteSearchStore(result['path']).snapshot().items)==4


def test_native_bundle_corruption_rollback(fixture):
    f=fixture
    original=ingest(f.ws,f.corpus,encoder=f.encoder)
    bundle=Path(original['path']).with_name('manifest.json')
    native=json.loads(bundle.read_text())
    native['vectors'][0]['embedding_text_sha256']='0'*64
    bad=f.ws.root/'bad-bundle.json';atomic_json(bad,native)
    with pytest.raises(ValueError,match='embedding_text_hash_mismatch'):
        ingest(f.ws,f.corpus,vectors=bad,expected_fingerprint=f.encoder.fingerprint,expected_dimension=2)
    assert f.ws.state('index')['identity']==original['identity']
    native=json.loads(bundle.read_text());native['encoder_fingerprint']='other'
    atomic_json(bad,native)
    with pytest.raises(PipelineError,match='encoder_profile_or_corpus_mismatch'):
        ingest(f.ws,f.corpus,vectors=bad,expected_fingerprint=f.encoder.fingerprint,expected_dimension=2)
    assert f.ws.state('index')['identity']==original['identity']


def test_import_failure_does_not_publish_pointer(fixture):
    f=fixture
    old=ingest(f.ws,f.corpus,encoder=f.encoder)
    with patch.object(SQLiteSearchStore,'import_bundle',side_effect=RuntimeError('storage')):
        with pytest.raises(RuntimeError):ingest(f.ws,f.corpus,encoder=f.encoder,grouping_threshold=0.8)
    assert f.ws.state('index')['identity']==old['identity']


def test_missing_merge_is_visible_and_no_raw_event_claim(fixture):
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    f.merge=None
    result=job(f)
    assert result['status']=='partial'
    assert result['counts']=={'deferred':2}
    assert not f.fake.requests
    assert not SavedDataStore(f.ws.root).get_feed('SYNTHETIC_FACTORY','cfo')
    assert SavedDataStore(f.ws.root).processing_state('SYNTHETIC_FACTORY','cfo')['status']=='partial'


@pytest.mark.parametrize('error,expected',[(BudgetExceeded('limit'),'budget_exhausted'),(LLMUnavailable('failed'),'analysis_failed')])
def test_technical_llm_failure_is_not_exclude(fixture,error,expected):
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    f.fake._responder=lambda request:error
    result=job(f)
    assert result['status']=='partial',result
    assert result['counts']=={expected:3}
    assert not f.ws.cards_for(result['id'])
    assert job(f)['reused']


def test_context_limit_has_no_calls(fixture):
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    result=job(f,analysis_policy={'max_input_tokens':10,'max_projection_chars':10})
    assert result['status']=='partial'
    assert result['counts']=={'context_too_large':3}
    assert not f.fake.requests


def test_offline_runtime_does_not_use_key_or_template(fixture):
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    offline,_=analyst_runtime(f.ws,live=False)
    f.analyst=offline
    result=job(f)
    assert result['counts']=={'analysis_failed':3}
    assert not f.ws.cards_for(result['id'])


def test_profile_missing_and_fingerprint_mismatch_visible(fixture):
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    result=run_job(f.ws,f.profiles,'MISSING','cfo',AS_OF,analyst=f.analyst)
    assert result['error_code']=='company_profile_missing'
    interests_path=f.ws.root/'companies/SYNTHETIC_FACTORY-interests.json'
    interests=json.loads(interests_path.read_text());interests['encoder_fingerprint']='other'
    atomic_json(interests_path,interests)
    manifest_path=f.ws.root/'companies/manifest.json'
    manifest=json.loads(manifest_path.read_text());manifest['companies'][0]['interests_sha256']=digest(interests)
    atomic_json(manifest_path,manifest)
    result=job(f)
    assert result['status']=='failed'
    assert result['error_code']=='interest_index_profile_mismatch'


def test_shared_quota_blocks_analyst_before_transport(fixture):
    from llm.cluster_merge import SQLiteQuota
    f=fixture
    quota=SQLiteQuota(f.ws.root/'quota-test.sqlite3',1)
    quota.reserve('merge:already-spent')
    fake=FakeLLM(lambda request:json.dumps(analyst_draft()))
    wrapper=QuotaLLM(fake,quota,f.ws.root)
    from app.analyst.llm_gateway import ChatRequest
    with pytest.raises(BudgetExceeded):
        wrapper.chat(ChatRequest(messages=[],model='z-ai/glm-5.3-flash'))
    assert not fake.requests


def test_all_exclude_is_completed_not_a_technical_failure(fixture):
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    f.fake._responder=lambda request:json.dumps(dict(decision='exclude',exclude_reason='Нет подтверждённой связи',event_status='occurred',
        headline='Событие не касается компании',what_happened=[dict(text='Источник сообщает о событии.',kind='fact',claim_ids=['C1'])]))
    result=job(f)
    assert result['status']=='ready'
    assert result['counts']=={'exclude':3}
    assert not f.ws.cards_for(result['id'])


def test_healthcheck_empty_healthy_failed_and_stale():
    from news.healthcheck import healthy
    current=datetime.now(timezone.utc)
    def state(status,ok=True,at=None):return dict(last_run=dict(ok=ok,completed_at=(at or current).isoformat(),collection=dict(status=status)))
    assert healthy(state('no_new'),every=60,current=current)
    assert healthy(state('collected'),every=60,current=current)
    assert not healthy(state('all_sources_failed',False),every=60,current=current)
    assert not healthy({},every=60,current=current)
    assert not healthy(state('no_new',at=current-timedelta(days=1)),every=60,current=current)


def test_scheduler_requires_structured_collection_result(tmp_path):
    from news.scheduler import _run_collect
    args=SimpleNamespace(target=0,category=None,out_dir=str(tmp_path),cache_dir=None,collect_timeout=5)
    with patch('news.scheduler.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='')):
        assert not _run_collect(args)['ok']


def test_full_cycle_claims_unknown_publication_without_fabricating_time(fixture):
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    output=job(f)
    card=next(c for c in f.ws.cards_for(output['id']) if c['feed']['event']['date'] is None)
    assert card['insight']['event']['date'] is None
    assert any(e['date'] is None for e in card['insight']['evidence'] if e['revision_id'])


@pytest.mark.parametrize('fail,expected',[(False,'no_new'),(True,'all_sources_failed')])
def test_collector_empty_result_distinguishes_source_failure(tmp_path,fail,expected):
    from news.dump_large import main
    spec=SimpleNamespace(name='synthetic-source',url='https://example.org/feed',language='ru')
    registry=SimpleNamespace(enabled=lambda *args:[spec])
    fetcher=SimpleNamespace(feed_failures=[],close=lambda:None)
    def candidates(_):
        if fail:raise RuntimeError('synthetic failure')
        return []
    fetcher.fetch_candidates=candidates
    with patch('news.core.registry.load_registry',return_value=registry),patch('news.core.fetcher.Fetcher',return_value=fetcher), \
         patch('sys.argv',['dump_large','--out-dir',str(tmp_path),'--target','0']):
        assert main()==(1 if fail else 0)
    report=json.loads((tmp_path/'collection-result.json').read_text())
    assert report['status']==expected
    assert report['new_articles']==0
    assert report['healthy_sources']==(0 if fail else 1)


def test_explicit_raw_fallback_is_marked_with_possible_duplicates(fixture):
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    f.merge=None
    result=job(f,raw_fallback=True)
    assert result['status']=='partial'
    assert result['counts']['deferred']==2
    cards=f.ws.cards_for(result['id'])
    assert len(cards)==4
    assert all(c['feed']['event']['processing_status']=='merge_fallback' for c in cards)
    assert all('raw_fallback_may_include_duplicates' in c['feed']['event']['limitations'] for c in cards)


def test_unresolved_merge_never_replaces_index(fixture):
    f=fixture
    imported=ingest(f.ws,f.corpus,encoder=f.encoder)
    def unresolved(messages,profile):
        fragments=json.loads(messages[1]['content'])['fragments']
        return dict(choices=[dict(finish_reason='stop',message=dict(content=json.dumps(dict(events=[],coverage=[
            dict(fragment_id=x['id'],kind='unresolved',reason='synthetic ambiguity') for x in fragments]))))])
    f.provider.complete=unresolved
    result=job(f)
    assert result['status']=='partial'
    assert not f.ws.cards_for(result['id'])
    with SQLiteSearchStore(imported['path']) as index:
        assert all(i.kind=='article' for i in index.snapshot().items)


def test_changed_profile_versions_hide_stale_feed(fixture):
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    result=job(f)
    assert len(f.ws.cards_for(result['id']))==3
    paths=f.ws.root/'companies'
    profile=json.loads((paths/'SYNTHETIC_FACTORY-profile.json').read_text());profile['profile_version']='next-version'
    interests=json.loads((paths/'SYNTHETIC_FACTORY-interests.json').read_text());interests['profile_version']='next-version';interests['interest_set_version']='next-interest-version'
    atomic_json(paths/'SYNTHETIC_FACTORY-profile.json',profile);atomic_json(paths/'SYNTHETIC_FACTORY-interests.json',interests)
    manifest=json.loads((paths/'manifest.json').read_text());entry=manifest['companies'][0]
    entry['profile_sha256']=digest(profile);entry['interests_sha256']=digest(interests);entry['display']['profile_version']='next-version'
    atomic_json(paths/'manifest.json',manifest)
    saved=SavedDataStore(f.ws.root)
    assert saved.processing_state('SYNTHETIC_FACTORY','cfo')['status']=='stale'
    assert saved.get_feed('SYNTHETIC_FACTORY','cfo')==[]
    assert saved.get_insight(f.ws.cards_for(result['id'])[0]['id'],'SYNTHETIC_FACTORY','cfo')


def test_restart_does_not_reload_model_or_repeat_parity(fixture):
    from app.full_cycle.runtime import LazyCPUEncoder
    f=fixture
    ingest(f.ws,f.corpus,encoder=f.encoder)
    assert job(f)['status']=='ready'
    lazy=LazyCPUEncoder('unused-reference','unused-model',f.ws.root/'encoder',
                        fingerprint=f.encoder.fingerprint,dimension=2)
    with patch('retriever.cpu_encoder.CPUEncoder',side_effect=AssertionError('unnecessary model/parity load')):
        assert not ingest(f.ws,f.corpus,encoder=lazy)['changed']
        assert run_job(f.ws,f.profiles,'SYNTHETIC_FACTORY','cfo',AS_OF,analyst=f.analyst,
                       encoder=lazy,merge=f.merge,k=10,max_merge_calls=10)['reused']


def test_lazy_encoder_verifies_actual_fingerprint_before_returning_vectors(tmp_path):
    from app.full_cycle.runtime import LazyCPUEncoder
    lazy=LazyCPUEncoder('reference','model',tmp_path,fingerprint='expected',dimension=2)
    model=SimpleNamespace(fingerprint='different',dimension=2,encode_event_texts=lambda texts:[[1.,0.]])
    with patch('retriever.cpu_encoder.CPUEncoder',return_value=model):
        with pytest.raises(PipelineError,match='encoder_profile_mismatch'):
            lazy.encode_event_texts(['new article'])
    assert lazy._encoder is None
