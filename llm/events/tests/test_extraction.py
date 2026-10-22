from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from jsonschema import Draft202012Validator
from llm.events.budget import CallLedger, validate_limits, maximum_cost
from llm.events.extraction import (parse_extraction, extraction_schema, build_processing_spec,
    build_request, profile_from_spec)
from llm.events.extraction_models import ProviderProfile, ExecutionLimits
from llm.events.extraction_cli import load_cli_env
from llm.events.migration import migrate_v1_to_v2, migrate_v2_to_v3, migrate_v3_to_v4
from llm.events.provider import OpenAIProvider, OfflineProvider, ProviderResponse, ProviderFailure
from llm.events.runner import ExtractionRunner, LeaseHeartbeat
from llm.events.storage import EventStore, StorageError, processing_hash
from llm.events.storage_schema import B2_DDL, B2_TABLES
from llm.events.schemas import ROOT
from llm.events.validation import article_sha256


class Clock:
    def __init__(self):self.value=datetime(2026,10,21,20,59,tzinfo=timezone.utc).timestamp()
    def __call__(self):return self.value
    def advance(self,n):self.value+=n


class NoHeartbeat:
    lost=False
    def __init__(self,*_):pass
    def __enter__(self):return self
    def __exit__(self,*_):pass


class ExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture=json.loads((ROOT/'llm/events/fixtures/extraction-response.b3.1.json').read_text())
        cls.source=json.loads((ROOT/'llm/events/fixtures/synthetic.json').read_text())['articles'][0]['article']

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'state.sqlite3';self.clock=Clock()
        self.store=EventStore(self.db,clock=self.clock)
        self.article=deepcopy(self.source)
        self.article['text']=self.fixture['evidence'][0]['quote']
        self.sid=self.store.import_batch(json.dumps([self.article],ensure_ascii=False).encode()).items[0].source_id
        self.profile=ProviderProfile(execution_mode='offline',model='offline:scripted',max_tokens=1024,
            limits_confirmed=True,max_input_tokens=20000,max_output_tokens=4096,context_window_tokens=24096)
        self.spec=build_processing_spec(self.profile)
        self.limits=ExecutionLimits(tariffs_confirmed=True,input_rub_per_million='1',
            output_rub_per_million='2',daily_quota_rub='1')

    def error(self,code,fn,*args,**kwargs):
        with self.assertRaises(StorageError) as caught:fn(*args,**kwargs)
        self.assertEqual(caught.exception.code,code)
        return caught.exception

    def response(self,content=None,finish='stop',usage=True,status=200,**message):
        body={'choices':[{'finish_reason':finish,'message':{'content':json.dumps(self.fixture,ensure_ascii=False) if content is None else content,**message}}]}
        if usage:body['usage']={'prompt_tokens':100,'completion_tokens':20,'completion_tokens_details':{'reasoning_tokens':19},'cost':12345}
        return ProviderResponse(body,status,{'retry-after':'10'} if status==429 else {})

    def runner(self,responses,**kwargs):
        provider=OfflineProvider(responses)
        return ExtractionRunner(self.store,provider,heartbeat=NoHeartbeat,**kwargs),provider

    def queue(self):return self.store.enqueue(self.sid,self.spec)

    def test_schema_and_raw_representation(self):
        response=parse_extraction(json.dumps(self.fixture))
        Draft202012Validator(extraction_schema()).validate(self.fixture)
        self.assertEqual(response.claims[1].scenario.kind,'baseline')
        self.assertEqual(response.claims[2].scenario.kind,'alternative')
        self.assertEqual([m.value_role for m in response.claims[1].measurements],['before','after'])
        self.assertEqual(response.claims[1].measurements[1].value_text,'0,5')
        self.assertEqual(len(response.claims[0].measurements),2)
        self.assertNotIn('start',response.claims[0].time.observation_period.model_dump())
        expected=json.loads((ROOT/'docs/contracts/extraction-response.b3.1.schema.json').read_text())
        expected.pop('$schema');self.assertEqual(expected,extraction_schema())

    def test_zero_claims_and_grounding_deferred(self):
        empty={'claims':[],'evidence':[],'excluded':[],'limitations':[]}
        self.assertEqual(parse_extraction(json.dumps(empty)).claims,[])
        item=deepcopy(self.fixture);item['evidence'][0]['quote']='absent from article 😀'
        # B3 must NOT mislabel structural acceptance as quote verification.
        self.assertEqual(parse_extraction(json.dumps(item)).evidence[0].quote,'absent from article 😀')

    def test_invalid_json_and_strict_schema(self):
        for text in ['{','```json\n{}\n```','{} {}','{"a":1,"a":2}','{"claims":NaN}']:
            self.error('invalid_extraction_json',parse_extraction,text)
        self.error('empty_response',parse_extraction,'')
        for mutate in [lambda x:x.update(extra=1),lambda x:x['claims'][0].update(polarity=1),
            lambda x:x['claims'][0]['measurements'][0].update(value_role='future'),
            lambda x:x['claims'][0]['time']['observation_period'].update(start='2026-01-01'),
            lambda x:x['claims'][0]['participants'][0].update(entity_id='invented')]:
            data=deepcopy(self.fixture);mutate(data)
            self.error('invalid_extraction_schema',parse_extraction,json.dumps(data))

    def test_local_ids_refs_and_pointers(self):
        mutations=[lambda x:x['claims'][1].update(claim_id='c1'),
            lambda x:x['evidence'].append(deepcopy(x['evidence'][0])),
            lambda x:x['claims'][1]['measurements'][0].update(measurement_id='m1'),
            lambda x:x['claims'][0].update(evidence_ids=['missing']),
            lambda x:x['claims'][0].update(field_evidence={'/absent':['e1']}),
            lambda x:x['claims'][0].update(field_evidence={'/measurements/-1':['e1']}),
            lambda x:x['claims'][0].update(field_evidence={'/text':['absent']}),
            lambda x:x['claims'][0].update(field_evidence={'/claim_id':['e1']})]
        for mutate in mutations:
            data=deepcopy(self.fixture);mutate(data)
            self.error('invalid_extraction_references',parse_extraction,json.dumps(data))

    def test_multiple_sentences_evidence_and_prompt(self):
        data=deepcopy(self.fixture)
        data['evidence'].append({'evidence_id':'e2','field':'title','quote':'Другой контекст 😀'})
        data['claims'][0]['evidence_ids'].append('e2')
        data['claims'][0]['field_evidence']['/text'].append('e2')
        self.assertEqual(len(parse_extraction(json.dumps(data)).evidence),2)
        record=self.store.get_article(self.sid)
        request=build_request(record,self.profile)
        self.assertIn('ARTICLE_DATA_JSON',request['messages'][1]['content'])
        self.assertIn(self.article['text'],request['messages'][1]['content'])
        self.assertNotIn('tools',request);self.assertFalse(request['stream'])
        self.assertNotIn('Authorization',json.dumps(request))

    def test_spec_cache_configuration_and_mode(self):
        self.assertEqual(build_processing_spec(self.profile),self.spec)
        for changes in [{'model':'offline:other'},{'thinking':'disabled'}, {'response_format':'json_object'}, {'max_tokens':1025}, {'execution_mode':'live'}]:
            self.assertNotEqual(processing_hash(build_processing_spec(self.profile.model_copy(update=changes))),processing_hash(self.spec))
        with patch('llm.events.extraction.SYSTEM_PROMPT','updated'):
            self.assertNotEqual(build_processing_spec(self.profile),self.spec)
            self.error('processing_spec_mismatch',profile_from_spec,self.spec)
        with patch('llm.events.extraction.extraction_schema',return_value={'type':'object'}):
            self.assertNotEqual(build_processing_spec(self.profile),self.spec)

    def test_success_artifacts_and_cache_reuse(self):
        job=self.queue();runner,provider=self.runner([self.response()])
        result=runner.run_next(self.spec,self.limits)
        self.assertEqual(result.status,'succeeded')
        cached=self.store.get_cached_result(self.sid,self.spec)
        self.assertEqual(cached.payload['verification_status'],'unverified')
        self.assertEqual(cached.payload['source_id'],self.sid)
        self.assertEqual(cached.payload['article_sha256'],article_sha256(self.store.get_article(self.sid).article))
        self.assertEqual(cached.schema_version,'b3.1')
        self.assertEqual(self.store.enqueue(self.sid,self.spec).job_id,job.job_id)
        self.assertEqual(runner.run_next(self.spec,self.limits).status,'idle')
        self.assertEqual(len(provider.requests),1)
        attempt=self.store.list_attempts(job.job_id)[0]
        self.assertEqual({a.kind for a in self.store.list_artifacts(attempt.attempt_id)},{'llm_request','llm_response','extraction_candidates'})

    def test_zero_claims_succeed(self):
        self.queue();runner,_=self.runner([self.response(json.dumps({'claims':[],'evidence':[],'excluded':[],'limitations':[]}))])
        self.assertEqual(runner.run_next(self.spec,self.limits).status,'succeeded')

    def test_length_empty_refusal_tools_and_malformed_fail(self):
        cases=[(self.response('{}',finish='length'),'output_limit_exceeded'),
            (self.response(''),'empty_response'),(self.response(refusal='no'),'provider_refusal'),
            (self.response(tool_calls=[{}]),'unexpected_tools'),(self.response('{'),'invalid_extraction_json'),
            (self.response('{}'),'invalid_extraction_schema'),(self.response(finish='other'),'unexpected_finish_reason')]
        job=self.queue()
        for response,reason in cases:
            if self.store.get_job(job.job_id).state=='failed':self.store.retry_job(job.job_id)
            runner,provider=self.runner([response]);result=runner.run_next(self.spec,self.limits)
            self.assertEqual((result.status,result.reason),('failed',reason))
            self.assertEqual(len(provider.requests),1)
            self.assertIsNone(self.store.get_cached_result(self.sid,self.spec))
        self.assertEqual(len(self.store.list_attempts(job.job_id)),len(cases))

    def test_http_errors_no_retries_and_stop(self):
        job=self.queue()
        for status in [429,500,401,403,400,302]:
            state=self.store.get_job(job.job_id).state
            if state in {'failed','deferred'}:self.store.retry_job(job.job_id)
            runner,provider=self.runner([self.response(status=status,usage=False)])
            result=runner.run_next(self.spec,self.limits)
            self.assertTrue(result.stop);self.assertEqual(len(provider.requests),1)
            self.assertEqual(result.status,'deferred' if status==429 else 'failed')
            call=runner.ledger.get(result.call_id)
            self.assertEqual(call['state'],'unknown')
            self.assertEqual(call['response']['headers'].get('retry-after'),'10' if status==429 else None)

    def test_timeout_keeps_reserve_and_no_success(self):
        self.queue();runner,provider=self.runner([ProviderFailure('provider_timeout','Cost uncertain')])
        result=runner.run_next(self.spec,self.limits)
        call=runner.ledger.get(result.call_id)
        self.assertEqual((call['state'],call['accounted_rub']),('unknown',call['reserved_rub']))
        self.assertEqual(result.status,'failed');self.assertTrue(result.stop)
        self.assertEqual(len(provider.requests),1)

    def test_long_article_deferred_without_truncation_or_request(self):
        profile=self.profile.model_copy(update={'max_request_bytes':1});spec=build_processing_spec(profile)
        self.store.enqueue(self.sid,spec);runner,provider=self.runner([])
        result=runner.run_next(spec,self.limits)
        self.assertEqual((result.status,result.reason),('deferred','input_too_large'))
        self.assertEqual(provider.requests,[]);self.assertEqual(runner.ledger.list_calls(),[])

    def test_live_gates_precede_claim_and_network(self):
        profile=ProviderProfile();spec=build_processing_spec(profile)
        job=self.store.enqueue(self.sid,spec)
        provider=OpenAIProvider(profile,api_key='fake-secret',transport=httpx.MockTransport(lambda req:self.fail('Network must not be attempted')))
        runner=ExtractionRunner(self.store,provider)
        self.error('live_calls_disabled',runner.run_next,spec,ExecutionLimits())
        self.error('budget_configuration_required',runner.run_next,spec,ExecutionLimits(live_enabled=True))
        self.assertEqual(self.store.get_job(job.job_id).state,'pending')
        offline,_=self.runner([])
        self.error('execution_mode_mismatch',offline.run_next,spec,self.limits)

    def test_unverified_capabilities_and_model_bounds(self):
        profile=self.profile.model_copy(update={'execution_mode':'live','model':'deepseek/deepseek-v4.1-flash'})
        limits=self.limits.model_copy(update={'live_enabled':True})
        for changes,code in [({'thinking':'disabled'},'unverified_thinking_parameter'),
            ({'response_format':'json_schema'},'unverified_response_format'),
            ({'max_tokens':5000},'incompatible_model_limits'),({'context_window_tokens':100},'incompatible_model_limits')]:
            self.error(code,validate_limits,profile.model_copy(update=changes),limits,live=True)

    def test_usage_decimal_cost_no_double_reasoning_or_opaque_cost(self):
        self.queue();runner,_=self.runner([self.response()]);result=runner.run_next(self.spec,self.limits)
        call=runner.ledger.get(result.call_id)
        self.assertEqual(Decimal(call['accounted_rub']),Decimal('0.00014'))
        self.assertEqual(call['response']['body']['usage']['cost'],12345)

    def test_missing_usage_holds_reservation_after_restart(self):
        self.queue();runner,_=self.runner([self.response(usage=False)]);result=runner.run_next(self.spec,self.limits)
        # Reopen with the SAME test clock: budget() keys by calendar day, and a
        # fresh EventStore with a real wall-clock would compute a different day,
        # making accounted_rub appear 0. Use self.clock to keep the day stable.
        reopened=CallLedger(EventStore(self.db, clock=self.clock),offline=True)
        call=reopened.get(result.call_id)
        self.assertEqual(call['state'],'unknown')
        self.assertEqual(Decimal(reopened.budget()['accounted_rub']),maximum_cost(self.profile,self.limits))
        reconciled=reopened.reconcile(result.call_id,'0.002','Provider invoice confirmed')
        self.assertEqual(reconciled['state'],'reconciled')
        self.error('invalid_call_state',reopened.reconcile,result.call_id,'0','again')
        self.assertEqual(len(reopened.list_calls()),1)

    def test_budget_concurrent_reservation_and_midnight_carry(self):
        profile=self.profile;limits=self.limits.model_copy(update={'daily_quota_rub':'0.03'})
        self.queue();first=self.store.claim_next(self.spec,'a')
        a=deepcopy(self.article);a['article_id']='second'
        sid=self.store.import_batch(json.dumps([a]).encode()).items[0].source_id
        self.store.enqueue(sid,self.spec);second=self.store.claim_next(self.spec,'b')
        tokens=[first.active_attempt.token,second.active_attempt.token]
        def reserve(token):
            try:
                return CallLedger(EventStore(self.db,clock=self.clock),offline=True).reserve(token,{},profile,limits)
            except StorageError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,tokens))
        self.assertEqual(results.count('budget_exhausted'),1)
        ledger=CallLedger(self.store,offline=True)
        self.assertEqual(len(ledger.list_calls()),1)
        self.clock.advance(120)  # Moscow midnight; keep reservation from previous day.
        self.assertEqual(ledger.budget()['budget_day'],'offline:2026-10-22')
        # Fresh attempt; lease on older ones can be expired independently.
        new=self.store.enqueue(sid,build_processing_spec(profile.model_copy(update={'max_tokens':1000})))
        claimed=self.store.claim_next(new.spec,'c')
        self.error('budget_exhausted',ledger.reserve,claimed.active_attempt.token,{},profile,limits)

    def test_unknown_reconciliation_requires_reason_and_inactive_attempt(self):
        self.queue();job=self.store.claim_next(self.spec,'a')
        ledger=CallLedger(self.store,offline=True)
        call=ledger.reserve(job.active_attempt.token,{},self.profile,self.limits)
        self.error('active_call',ledger.reconcile,call,'0','not yet')
        self.error('invalid_argument',ledger.reconcile,call,'0','')
        self.clock.advance(901);self.store.requeue_expired()
        ledger.reconcile(call,'0','Executor stopped; confirmed no dispatch')
        self.assertEqual(ledger.get(call)['accounted_rub'],'0')

    def test_overrun_blocks_future_calls_until_acknowledged(self):
        self.queue();response=self.response();response.body['usage']['completion_tokens']=1000000
        runner,_=self.runner([response]);result=runner.run_next(self.spec,self.limits)
        self.assertTrue(result.stop);self.assertTrue(runner.ledger.budget()['blocked'])
        runner.ledger.acknowledge_overrun(runner.ledger.budget()['budget_day'],'Tariffs and actual provider bounds reviewed')
        self.assertFalse(runner.ledger.budget()['blocked'])

    def test_offline_budget_and_cache_do_not_affect_live(self):
        self.queue();runner,_=self.runner([self.response(usage=False)])
        runner.run_next(self.spec,self.limits)
        self.assertEqual(CallLedger(self.store).budget()['accounted_rub'],'0')
        live=build_processing_spec(self.profile.model_copy(update={'execution_mode':'live'}))
        self.assertIsNone(self.store.get_cached_result(self.sid,live))

    def test_crash_after_reservation_does_not_release_money(self):
        self.queue();runner,_=self.runner([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):runner.run_next(self.spec,self.limits)
        calls=runner.ledger.list_calls();self.assertEqual(calls[0]['state'],'reserved')
        self.clock.advance(901);self.store.requeue_expired()
        self.assertEqual(runner.ledger.get(calls[0]['call_id'])['state'],'reserved')

    def test_revision_while_api_running_records_cost_not_success(self):
        self.queue();outer=self
        class UpdatingProvider(OfflineProvider):
            def complete(self,request):
                article=deepcopy(outer.article);article['text']+=' Исправление.'
                outer.store.import_batch(json.dumps([article],ensure_ascii=False).encode())
                return outer.response()
        runner=ExtractionRunner(self.store,UpdatingProvider([]),heartbeat=NoHeartbeat)
        result=runner.run_next(self.spec,self.limits)
        self.assertEqual(result.status,'superseded')
        self.assertIsNone(self.store.get_cached_result(self.sid,self.spec))
        self.assertEqual(runner.ledger.get(result.call_id)['state'],'settled')

    def test_expiry_during_response_and_heartbeat_pulse(self):
        self.queue();outer=self
        class ExpiringProvider(OfflineProvider):
            def complete(self,request):outer.clock.advance(901);return outer.response()
        runner=ExtractionRunner(self.store,ExpiringProvider([]),heartbeat=NoHeartbeat)
        result=runner.run_next(self.spec,self.limits)
        self.assertEqual(result.status,'superseded')
        self.assertEqual(runner.ledger.get(result.call_id)['state'],'settled')
        self.store.requeue_expired();job=self.store.claim_next(self.spec,'next')
        heartbeat=LeaseHeartbeat(self.store,job.active_attempt.token)
        self.clock.advance(100);heartbeat.pulse()
        self.assertGreater(self.store.get_job(job.job_id).active_attempt.lease_expires_at,job.active_attempt.lease_expires_at)
        self.clock.advance(901);heartbeat.pulse();self.assertTrue(heartbeat.lost)

    def test_run_pending_continues_article_error_but_has_finite_cap(self):
        self.queue()
        second=deepcopy(self.article);second['article_id']='second'
        sid=self.store.import_batch(json.dumps([second]).encode()).items[0].source_id
        self.store.enqueue(sid,self.spec)
        runner,provider=self.runner([self.response('{'),self.response()])
        results=runner.run_pending(self.spec,self.limits,2)
        self.assertEqual([r.status for r in results],['failed','succeeded'])
        self.assertEqual(len(provider.requests),2)
        self.error('invalid_argument',runner.run_pending,self.spec,self.limits,0)

    def test_succeeded_result_status_atomic_failure_keeps_response_audit(self):
        self.queue();runner,_=self.runner([self.response()])
        with patch.object(self.store,'complete_attempt',side_effect=StorageError('storage_busy','Busy')):
            self.error('storage_busy',runner.run_next,self.spec,self.limits)
        self.assertEqual(runner.ledger.list_calls()[0]['state'],'settled')
        self.assertIsNone(self.store.get_cached_result(self.sid,self.spec))

    def legacy_database(self):
        path=Path(self.temp.name)/'legacy.sqlite3'
        with ExitStack() as stack:
            for name,value in [('DDL',B2_DDL),('TABLES',B2_TABLES),('SCHEMA_VERSION',1)]:
                stack.enter_context(patch('llm.events.storage.'+name,value))
            store=EventStore(path,clock=self.clock)
            receipt=store.import_batch(json.dumps([self.article]).encode())
            record=store.get_article(receipt.items[0].source_id)
        return path,record

    def test_explicit_migration_backup_archive_and_foreign_rejection(self):
        path,record=self.legacy_database()
        self.error('storage_migration_required',EventStore,path)
        backup=Path(self.temp.name)/'snapshot.sqlite3'
        result=migrate_v1_to_v2(path,backup)
        self.assertEqual(result['schema_version'],2)
        self.error('storage_migration_required',EventStore,path)
        migrate_v2_to_v3(path)
        migrate_v3_to_v4(path)
        self.assertEqual(EventStore(path).get_article(record.source_id),record)
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM article_versions').fetchone()[0],1)
        self.error('unsupported_storage_schema',migrate_v1_to_v2,path)
        foreign=Path(self.temp.name)/'foreign.sqlite3'
        with closing(sqlite3.connect(foreign)) as conn:conn.execute('CREATE TABLE secrets(x)')
        self.error('unsupported_storage_schema',migrate_v1_to_v2,foreign)

    def test_migration_failure_rolls_back_and_existing_backup_not_overwritten(self):
        path,_=self.legacy_database();backup=Path(self.temp.name)/'v1.backup'
        with patch('llm.events.migration.B3_DDL',['CREATE TABLE temp_new(x)','BROKEN SQL']):
            self.error('storage_error',migrate_v1_to_v2,path,backup)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],1)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='temp_new'").fetchone())
        data=backup.read_bytes()
        self.error('storage_io_error',migrate_v1_to_v2,path,backup)
        self.assertEqual(backup.read_bytes(),data)

    def test_call_audit_immutability_and_single_call_per_attempt(self):
        self.queue();job=self.store.claim_next(self.spec,'a');ledger=CallLedger(self.store,offline=True)
        call=ledger.reserve(job.active_attempt.token,{},self.profile,self.limits)
        self.error('attempt_already_called',ledger.reserve,job.active_attempt.token,{},self.profile,self.limits)
        ledger.finish(call,response=self.response().payload())
        self.error('call_already_finished',ledger.finish,call,response=self.response().payload())
        with closing(sqlite3.connect(self.db)) as conn:
            for sql in ["DELETE FROM llm_calls","UPDATE llm_calls SET request_json='{}'","UPDATE llm_calls SET response_json='{}'"]:
                with self.assertRaises(sqlite3.IntegrityError):conn.execute(sql)

    def test_invalid_money_quota_and_no_live_calls_by_default(self):
        for value in [None,'NaN','Infinity','-1','1e999999']:
            limits=self.limits.model_copy(update={'input_rub_per_million':value})
            self.error('invalid_budget_configuration',validate_limits,self.profile,limits,live=False)
        for value in ['0','501']:
            self.error('invalid_budget_configuration',validate_limits,self.profile,self.limits.model_copy(update={'daily_quota_rub':value}),live=False)

    def test_sdk_mock_no_retries_redaction_and_settings(self):
        requests=[];secret='fake-sdk-key-never-live'
        profile=ProviderProfile(thinking='disabled')
        def handle(request):
            requests.append(request)
            self.assertEqual(request.headers['authorization'],'Bearer '+secret)
            self.assertEqual(json.loads(request.content)['thinking'],{'type':'disabled'})
            return httpx.Response(429,json={'error':{'message':secret},'api_key':secret},headers={'Retry-After':'10'},request=request)
        provider=OpenAIProvider(profile,api_key=secret,transport=httpx.MockTransport(handle))
        record=self.store.get_article(self.sid)
        response=provider.complete(build_request(record,profile))
        self.assertEqual(len(requests),1);self.assertEqual(response.status_code,429)
        self.assertNotIn(secret,json.dumps(response.payload()))
        self.assertEqual(response.headers['retry-after'],'10')

    def test_sdk_timeout_no_retry(self):
        requests=[]
        def handle(request):requests.append(request);raise httpx.ReadTimeout('timeout',request=request)
        provider=OpenAIProvider(ProviderProfile(),api_key='fake',transport=httpx.MockTransport(handle))
        with self.assertRaises(ProviderFailure) as caught:provider.complete(build_request(self.store.get_article(self.sid),ProviderProfile()))
        self.assertEqual(caught.exception.code,'provider_timeout');self.assertEqual(len(requests),1)

    def test_env_priority_and_cli_errors_do_not_echo_secret(self):
        env=Path(self.temp.name)/'.env';env.write_text('LLM_API_KEY="file-secret"\n')
        with patch.dict(os.environ,{'LLM_API_KEY':'process-secret'}):
            load_cli_env(env);self.assertEqual(os.environ['LLM_API_KEY'],'process-secret')
        with patch.dict(os.environ,{},clear=True):
            load_cli_env(env);self.assertEqual(os.environ['LLM_API_KEY'],'file-secret')
        path=Path(self.temp.name)/'bad-profile.json';path.write_text('{"api_key":"never-print-me"}')
        command=[sys.executable,'-m','llm.events.extraction_cli','validate-profile','--profile',str(path),'--limits',str(ROOT/'llm/events/profiles/limits.example.json')]
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,1);self.assertNotIn('never-print-me',result.stdout+result.stderr)

    def test_cli_offline_end_to_end_and_live_gate(self):
        command=[sys.executable,'-m','llm.events.extraction_cli']
        profile=ROOT/'llm/events/profiles/offline.json'
        args=['--db',str(self.db),'--profile',str(profile)]
        queued=subprocess.run(command+['enqueue',*args,'--source-id',self.sid],capture_output=True,text=True)
        self.assertEqual(queued.returncode,0,queued.stderr+queued.stdout)
        result=subprocess.run(command+['run',*args,'--limits',str(ROOT/'llm/events/profiles/offline-limits.json'),
            '--offline-responses',str(ROOT/'llm/events/profiles/offline-responses.json'),'--max-jobs','1'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)[0]['status'],'succeeded')
        check=subprocess.run(command+['validate-profile','--profile',str(ROOT/'llm/events/profiles/provider.example.json'),
            '--limits',str(ROOT/'llm/events/profiles/limits.example.json')],capture_output=True,text=True)
        self.assertEqual(check.returncode,1);self.assertIn('live_calls_disabled',check.stdout)

    def test_provider_success_thinking_body_and_raw_response(self):
        requests=[]
        profile=self.profile.model_copy(update={'execution_mode':'live','model':'deepseek/deepseek-v4.1-flash'})
        def handle(request):
            requests.append(request)
            return httpx.Response(200,json=self.response().body,headers={'x-request-id':'test-id'},request=request)
        provider=OpenAIProvider(profile,api_key='fake-key',transport=httpx.MockTransport(handle))
        spec=build_processing_spec(profile);self.store.enqueue(self.sid,spec)
        limits=self.limits.model_copy(update={'live_enabled':True})
        runner=ExtractionRunner(self.store,provider,heartbeat=NoHeartbeat)
        result=runner.run_next(spec,limits)
        self.assertEqual(result.status,'succeeded');self.assertEqual(len(requests),1)
        call=runner.ledger.get(result.call_id)
        self.assertIsInstance(call['response']['raw_body'],str)
        self.assertEqual(call['response']['headers']['x-request-id'],'test-id')
        self.assertEqual(json.loads(call['response']['raw_body']),self.response().body)

    def test_provider_invalid_envelope_stays_auditable(self):
        for content in ['not JSON','{"usage": NaN}','[]']:
            def handle(request):return httpx.Response(200,text=content,request=request)
            provider=OpenAIProvider(ProviderProfile(),api_key='fake-key',transport=httpx.MockTransport(handle))
            result=provider.complete(build_request(self.store.get_article(self.sid),ProviderProfile()))
            self.assertEqual(result.raw_body,content)
            self.assertIsInstance(result.body,dict)

    def test_probe_allows_unverified_capability_but_not_missing_tariffs(self):
        profile=self.profile.model_copy(update={'execution_mode':'live','model':'deepseek/deepseek-v4.1-flash',
            'thinking':'disabled','thinking_verified':False})
        spec=build_processing_spec(profile)
        seen=[]
        def handle(request):
            seen.append(request)
            return httpx.Response(200,json=self.response().body,request=request)
        provider=OpenAIProvider(profile,api_key='fake',transport=httpx.MockTransport(handle))
        runner=ExtractionRunner(self.store,provider,heartbeat=NoHeartbeat)
        limits=self.limits.model_copy(update={'live_enabled':True})
        self.error('budget_configuration_required',runner.run_next,spec,
            limits.model_copy(update={'tariffs_confirmed':False}),_probe=True)
        self.assertEqual(seen,[])
        self.store.enqueue(self.sid,spec)
        self.assertEqual(runner.run_next(spec,limits,_probe=True,source_id=self.sid).status,'succeeded')
        self.assertEqual(len(seen),1)

    def test_retry_new_attempt_not_automatic_after_missing_usage(self):
        job=self.queue();runner,provider=self.runner([self.response('{',usage=False),self.response()])
        result=runner.run_next(self.spec,self.limits)
        self.assertEqual(result.status,'failed');self.assertEqual(len(provider.requests),1)
        self.assertEqual(runner.run_next(self.spec,self.limits).status,'idle')
        self.store.retry_job(job.job_id)
        second=runner.run_next(self.spec,self.limits)
        self.assertEqual(second.status,'succeeded')
        self.assertNotEqual(second.call_id,result.call_id)
        self.assertEqual(len(runner.ledger.list_calls()),2)

    def test_budget_exhaustion_defers_job_and_does_not_dispatch(self):
        self.queue();runner,provider=self.runner([])
        result=runner.run_next(self.spec,self.limits.model_copy(update={'daily_quota_rub':'0.00001'}))
        self.assertEqual((result.status,result.reason,result.stop),('deferred','budget_exhausted',True))
        self.assertEqual(provider.requests,[])
        self.assertIsNone(self.store.get_cached_result(self.sid,self.spec))

    def test_quota_change_cannot_raise_existing_daily_budget(self):
        self.queue();runner,_=self.runner([self.response()]);runner.run_next(self.spec,self.limits)
        profile=self.profile.model_copy(update={'max_tokens':1000});spec=build_processing_spec(profile)
        self.store.enqueue(self.sid,spec)
        new,provider=self.runner([])
        result=new.run_next(spec,self.limits.model_copy(update={'daily_quota_rub':'2'}))
        self.assertEqual(result.reason,'budget_quota_mismatch');self.assertTrue(result.stop)
        self.assertEqual(provider.requests,[])

    def test_late_response_after_manual_reconciliation_preserves_both(self):
        self.queue();job=self.store.claim_next(self.spec,'one');ledger=CallLedger(self.store,offline=True)
        call=ledger.reserve(job.active_attempt.token,{},self.profile,self.limits)
        self.clock.advance(901);self.store.requeue_expired()
        ledger.reconcile(call,'0.001','Confirmed provider invoice')
        ledger.finish(call,response=self.response().payload())
        row=ledger.get(call)
        self.assertEqual(row['state'],'reconciled');self.assertEqual(row['accounted_rub'],'0.001')
        self.assertIsNotNone(row['response']);self.assertIsNotNone(row['finished_at'])

    def test_claim_filter_does_not_process_unselected_probe_article(self):
        job=self.queue()
        other=deepcopy(self.article);other['article_id']='unselected'
        sid=self.store.import_batch(json.dumps([other]).encode()).items[0].source_id
        other_job=self.store.enqueue(sid,self.spec)
        runner,_=self.runner([self.response()])
        result=runner.run_next(self.spec,self.limits,source_id=self.sid)
        self.assertEqual(result.job_id,job.job_id)
        self.assertEqual(self.store.get_job(other_job.job_id).state,'pending')
