from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date,datetime,timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from jsonschema import Draft202012Validator
from llm.events.extraction.budget import CallLedger
from llm.events.extraction.protocol import build_processing_spec, digest
from llm.events.extraction.models import ExtractionResult, ProviderProfile, ExecutionLimits
from llm.events.fixtures.make_normalization import case,claim,measurement
from llm.events.contracts.models import ArticleRecord, Issue
from llm.events.normalization.core import normalize_extraction, validate_normalized_result, normalization_schema, configuration_hash, resolved_dictionary, exact_span, alias
from llm.events.normalization.dates import normalize_period
from llm.events.normalization.models import NormalizationConfig
from llm.events.normalization.numbers import normalize_measurement
from llm.events.extraction.models import CandidateMeasurement
from llm.events.normalization.service import NormalizationService
from llm.events.extraction.provider import OfflineProvider, ProviderResponse, ProviderFailure
from llm.events.extraction.runner import ExtractionRunner
from llm.events.contracts.schemas import ROOT
from llm.events.storage.store import EventStore, StorageError, processing_hash
from llm.events.storage.schema import SCHEMA_VERSION
from llm.events.contracts.validation import validate_claim_collection, validate_grounding, ContractValidationError, article_sha256


class NoHeartbeat:
    lost=False
    def __init__(self,*_):pass
    def __enter__(self):return self
    def __exit__(self,*_):pass


class Clock:
    def __init__(self):self.value=datetime(2026,10,21,tzinfo=timezone.utc).timestamp()
    def __call__(self):return self.value


class NormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=json.loads((ROOT/'llm/events/fixtures/normalization.b4.1.json').read_text())
        cls.by_name={c['name']:c for c in cls.cases}

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.clock=Clock();self.store=EventStore(Path(self.temp.name)/'state.sqlite3',clock=self.clock)
        self.service=NormalizationService(self.store)
        self.profile=ProviderProfile(candidate_format='b3.1',execution_mode='offline',model='offline:scripted',limits_confirmed=True,
            max_input_tokens=30000,max_output_tokens=4096,context_window_tokens=34096)
        self.limits=ExecutionLimits(tariffs_confirmed=True,input_rub_per_million='0',output_rub_per_million='0',daily_quota_rub='1')

    def error(self,code,fn,*args,**kwargs):
        with self.assertRaises(ContractValidationError) as caught:fn(*args,**kwargs)
        self.assertIn(code,[i.code for i in caught.exception.issues])

    def normalized(self,name):
        c=self.by_name[name];return normalize_extraction(c['input'],c['article'])

    def response(self,data):
        return ProviderResponse({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data,ensure_ascii=False) if not isinstance(data,str) else data}}],
            'usage':{'prompt_tokens':100,'completion_tokens':100}})

    def publish(self,c=None,*,content=None):
        c=deepcopy(c or self.by_name['percent'])
        sid=self.store.import_batch(json.dumps([c['article']['article']],ensure_ascii=False).encode()).items[0].source_id
        spec=build_processing_spec(self.profile);job=self.store.enqueue(sid,spec)
        transport=OfflineProvider([self.response(content if content is not None else c['input']['extraction'])])
        runner=ExtractionRunner(self.store,transport,heartbeat=NoHeartbeat)
        outcome=runner.run_next(spec,self.limits,source_id=sid)
        owner=self.store.get_job(job.job_id)
        return owner.result_artifact_id,outcome,owner,c

    def test_all_hand_authored_cases(self):
        for c in self.cases:
            with self.subTest(c['name']):
                r=normalize_extraction(c['input'],c['article']);e=c['expected']
                self.assertEqual(r,validate_normalized_result(r,c['article']))
                Draft202012Validator(normalization_schema()).validate(r.model_dump(mode='json'))
                if 'values' in e:
                    self.assertEqual([m['value'] for a in r.model_dump(mode='json')['accepted'] for m in a['claim']['measurements']],e['values'])
                if 'units' in e:self.assertEqual([m.unit for a in r.accepted for m in a.claim.measurements],e['units'])
                if 'quality' in e:self.assertEqual(r.quality,e['quality'])
                if 'accepted' in e:self.assertEqual(len(r.accepted),e['accepted'])
                if 'rejected' in e:self.assertIn(e['rejected'],[i.code for rejected in r.rejected for i in rejected.issues])
                if 'warning' in e:self.assertIn(e['warning'],[d.issue.code for d in r.diagnostics])
                if 'period_start' in e:self.assertEqual(r.accepted[0].claim.time.observation_period.start,e['period_start'])
                if 'entity_id' in e:self.assertEqual(r.accepted[0].claim.participants[0].entity_id,e['entity_id'])

    def test_schema_generation_and_b1_b3_contracts_unchanged(self):
        expected=json.loads((ROOT/'docs/contracts/normalized-article.b4.1.schema.json').read_text());expected.pop('$schema')
        self.assertEqual(expected,normalization_schema())
        r=self.normalized('percent');record=self.by_name['percent']['article']
        validate_claim_collection([a.claim for a in r.accepted],[r.source],r.evidence,[record])
        self.assertEqual(r.semantic_support,'not_assessed')

    def test_unicode_nbsp_coordinates_and_title(self):
        c=deepcopy(self.by_name['unicode']);r=normalize_extraction(c['input'],c['article'])
        e=r.evidence[0];text=c['article']['article']['text']
        self.assertEqual(text[e.start:e.end],e.quote);self.assertIn('😀',e.quote);self.assertIn('\u00a0',e.quote)
        c['article']['article']['title']=text;c['input']['extraction']['evidence'][0]['field']='title'
        c['input']['article_sha256']=article_sha256(__import__('llm.events.contracts.models',fromlist=['ArticleInput']).ArticleInput.model_validate(c['article']['article']))
        self.assertEqual(normalize_extraction(c['input'],c['article']).evidence[0].field,'title')
        self.error('quote_not_found',exact_span,ArticleRecord.model_validate(c['article']),'text',text.replace('\u00a0',' '))

    def test_same_input_stable_ids_different_config_changes_ids(self):
        c=self.by_name['percent'];original=deepcopy(c)
        a=normalize_extraction(c['input'],c['article']);b=normalize_extraction(c['input'],c['article'])
        self.assertEqual(a,b);self.assertEqual(c,original)
        config=NormalizationConfig(input_artifact_id='different')
        self.assertNotEqual(normalize_extraction(c['input'],c['article'],config).accepted[0].claim.claim_id,a.accepted[0].claim.claim_id)
        dictionary=resolved_dictionary(NormalizationConfig());dictionary['version']='test:changed'
        config=NormalizationConfig(dictionary=dictionary)
        self.assertNotEqual(normalize_extraction(c['input'],c['article'],config).accepted[0].claim.claim_id,a.accepted[0].claim.claim_id)

    def test_pure_historical_record_and_source_hash_mismatch(self):
        c=deepcopy(self.by_name['percent']);c['input']['article_sha256']='wrong'
        self.error('source_version_mismatch',normalize_extraction,c['input'],c['article'])
        c=deepcopy(self.by_name['percent']);c['input']['source_id']='wrong'
        self.error('source_version_mismatch',normalize_extraction,c['input'],c['article'])

    def test_nonexisting_and_ambiguous_evidence_reject_shared_claims(self):
        c=deepcopy(self.by_name['partial']);c['input']['extraction']['evidence'][0]['quote']='absent'
        r=normalize_extraction(c['input'],c['article']);self.assertEqual(len(r.rejected),2)
        self.assertEqual(r.accepted,[]);self.assertEqual(r.evidence,[])

    def test_no_automatic_evidence_completion_and_whole_claim_rejection(self):
        c=deepcopy(self.by_name['percent'])
        second=measurement('15','%');second['measurement_id']='m2'
        c['input']['extraction']['claims'][0]['measurements'].append(second)
        r=normalize_extraction(c['input'],c['article']);self.assertEqual(r.quality,'insufficient');self.assertFalse(r.accepted)
        self.assertIn('number_not_quoted',[i.code for x in r.rejected for i in x.issues])
        self.assertEqual(self.normalized('uncovered').rejected[0].issues[0].code,'uncovered_field')

    def test_scalar_field_paths_are_translated_and_period_children_inherit(self):
        c=deepcopy(self.by_name['past_september']);claim=c['input']['extraction']['claims'][0]
        claim['field_evidence'].pop('/time');claim['field_evidence']['/time/observation_period/text']=['e1']
        r=normalize_extraction(c['input'],c['article']);paths=r.accepted[0].claim.field_evidence
        self.assertIn('/time/observation_period/start',paths);self.assertIn('/time/observation_period/end',paths)
        c=deepcopy(self.by_name['percent']);claim=c['input']['extraction']['claims'][0];claim['field_evidence'].pop('/measurements')
        for k in ('metric','kind','value_text','unit_text','qualifier','value_role'):
            claim['field_evidence']['/measurements/0/'+k]=['e1']
        r=normalize_extraction(c['input'],c['article']);paths=r.accepted[0].claim.field_evidence
        self.assertIn('/measurements/0/value',paths);self.assertNotIn('/measurements/0/value_text',paths)

    def test_excluded_and_orphan_evidence_diagnostics(self):
        c=deepcopy(self.by_name['percent']);c['input']['extraction']['excluded']=[{'field':'text','quote':'absent','reason':'test'}]
        r=normalize_extraction(c['input'],c['article']);self.assertEqual(len(r.accepted),1);self.assertEqual(r.quality,'partial')
        c['input']['extraction']['excluded'][0]['quote']=c['article']['article']['text']
        self.assertEqual(len(normalize_extraction(c['input'],c['article']).excluded),1)

    def test_explicit_dates_accumulated_period_and_year_end(self):
        for text,start,end in [('23 октября 2026 года','2026-10-23','2026-10-23'),
            ('2026-10-23','2026-10-23','2026-10-23'),('23.10.2026','2026-10-23','2026-10-23'),
            ('январь—сентябрь 2026 года','2026-01-01','2026-09-30'),
            ('за девять месяцев 2026 года','2026-01-01','2026-09-30'),
            ('III квартал 2026 года','2026-07-01','2026-09-30'),
            ('первое полугодие 2026 года','2026-01-01','2026-06-30'),
            ('до конца года',None,'2026-12-31'),('на конец года','2026-12-31','2026-12-31'),
            ('в прошлом году','2025-01-01','2025-12-31')]:
            with self.subTest(text):
                r=normalize_period(text,'2026-10-20T12:00:00+03:00');self.assertEqual((r.period['start'],r.period['end']),(start,end));self.assertEqual(r.period['text'],text)

    def test_nearest_period_ties_missing_anchor_explicit_context_and_invalid_dates(self):
        # Equidistant from Sep 30 2025 and Sep 1 2026.
        left=date(2025,9,30);right=date(2026,9,1)
        midpoint=left+(right-left)/2
        if (right-left).days%2==0:
            self.assertIn('nearest_period_year_tie',normalize_period('в сентябре',midpoint.isoformat()+'T12:00:00+03:00').warnings)
        self.assertIn('publication_anchor_missing',normalize_period('в сентябре',None).warnings)
        self.assertEqual(normalize_period('в сентябре',None,context='По итогам 2025 года').period['start'],'2025-09-01')
        self.error('invalid_date',normalize_period,'31 февраля 2026 года',None)
        self.error('invalid_date',normalize_period,'31 февраля',None)
        self.assertIn('nearest_period_year_unavailable',normalize_period('29 февраля','2026-03-01T12:00:00+03:00').warnings)
        self.error('invalid_date',normalize_period,'2026-02-29',None)
        self.error('invalid_date',normalize_period,'5 квартал 2026 года',None)
        self.assertIsNone(normalize_period('после отчётного периода',None).period['start'])

    def test_comparison_bases_and_assumption_propagation(self):
        obs={'start':'2026-09-01','end':'2026-09-30'}
        r=normalize_period('год к году',None,observation=obs)
        self.assertEqual((r.period['start'],r.period['end']),('2025-09-01','2025-09-30'))
        self.assertEqual(normalize_period('к предыдущему месяцу',None,observation=obs).period['start'],'2026-08-01')
        self.assertIsNone(normalize_period('год к году',None).period['start'])

    def test_alias_conditions_unknown_similar_and_conflicting(self):
        dictionary=resolved_dictionary(NormalizationConfig())
        self.assertEqual(alias('ЦБ','ЦБ в России',dictionary,'entity_aliases')[0],'org:bank_of_russia')
        self.assertIsNone(alias('ЦБ','ЦБ Китая',dictionary,'entity_aliases')[0])
        self.assertIsNone(alias('Газпром нефть','Газпром',dictionary,'entity_aliases')[0])
        self.assertIsNone(alias('SYNTHETIC_U','Россия',dictionary,'entity_aliases')[0])
        self.assertEqual(alias('чистая прибыль','Чистая прибыль по РСБУ',dictionary,'metric_aliases')[0],'net_profit_ras')
        self.assertEqual(alias('чистая прибыль','Чистая прибыль по МСФО',dictionary,'metric_aliases')[0],'net_profit_ifrs')
        self.assertIsNone(alias('чистая прибыль','РСБУ и МСФО',dictionary,'metric_aliases')[0])
        duplicate=deepcopy(dictionary['entity_aliases'][0]);duplicate['entity_id']='org:other';dictionary['entity_aliases'].append(duplicate)
        self.assertIsNone(alias('Банк России','',dictionary,'entity_aliases')[0])
        dictionary['entity_aliases'][0]['condition']='ask_llm'
        self.error('invalid_dictionary',resolved_dictionary,NormalizationConfig(dictionary=dictionary))

    def numeric(self,text,value,unit,**kwargs):
        m=CandidateMeasurement.model_validate(dict(measurement(value,unit,**kwargs),measurement_id='m1'))
        return normalize_measurement(m,[text],[text])[:2]

    def test_multipliers_separators_ranges_and_explicit_sign(self):
        self.assertEqual(self.numeric('Стоимость — 1\u202f234,50 рублей.','1\u202f234,50','рублей'),('1234.5','RUB'))
        self.assertEqual(self.numeric('Доход — 1,31 млрд рублей.','1,31 млрд','млрд рублей'),('1310000000','RUB'))
        self.assertEqual(self.numeric('Доход — 185-190 млрд рублей.','185-190','млрд рублей')[0],{'min':'185000000000','max':'190000000000'})
        self.assertEqual(self.numeric('Снижение на −7%.','−7','%',kind='relative_change')[0],'-7')
        self.error('direction_mismatch',self.numeric,'Рост на −7%.','−7','%',kind='relative_change')
        self.error('conflicting_scale',self.numeric,'Доход 1,31 млрд рублей.','1,31 млрд','млн рублей')
        self.error('kind_unit_mismatch',self.numeric,'Снижение на 1 процентный пункт.','1','процентный пункт',kind='relative_change')
        self.error('qualifier_mismatch',self.numeric,'Около 12%.','12','%',qualifier='exact')
        self.assertEqual(self.numeric('Снижение примерно на 0,5%.','0,5','%',kind='relative_change',qualifier='approximate')[0],'-0.5')

    def test_service_cache_insufficient_and_current_revision(self):
        aid,outcome,owner,c=self.publish();self.assertEqual(outcome.status,'succeeded')
        result=self.service.normalize_artifact(aid);self.assertEqual(result['status'],'succeeded')
        repeated=self.service.normalize_artifact(aid);self.assertEqual(repeated['status'],'cached')
        self.assertEqual(result['artifact_id'],repeated['artifact_id'])
        aid2,_,_,_=self.publish(self.by_name['uncovered']);bad=self.service.normalize_artifact(aid2)
        self.assertEqual((bad['status'],bad['quality']),('failed','insufficient'))
        self.assertEqual(self.store.get_artifact(bad['artifact_id']).kind,'normalization_result')
        article=deepcopy(c['article']['article']);article['text']+=' Исправление.'
        self.store.import_batch(json.dumps([article],ensure_ascii=False).encode())
        self.error('article_not_current',self.service.normalize_artifact,aid)

    def test_service_lineage_mismatch_and_batch_bound(self):
        aid,_,owner,c=self.publish()
        with patch.object(self.store,'get_artifact',return_value=self.store.get_artifact(aid).model_copy(update={'kind':'raw_response'})):
            self.error('invalid_normalization_input',self.service.normalize_artifact,aid)
        results=self.service.normalize_batch([aid,'missing'],max_jobs=1);self.assertEqual(len(results),1)
        self.error('invalid_argument',self.service.normalize_batch,[aid],max_jobs=0)

    def test_repair_is_explicit_then_rechecked_and_chain_forbidden(self):
        aid,_,owner,c=self.publish(self.by_name['uncovered'])
        repaired=deepcopy(c['input']['extraction']);repaired['claims'][0]['field_evidence']['/modality']=['e1']
        provider=OfflineProvider([self.response(repaired)])
        result=self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(result['status'],'succeeded');self.assertEqual(len(provider.requests),1)
        self.assertEqual(result['normalization']['status'],'succeeded')
        self.error('repair_chain_forbidden',self.service.repair_input,{'artifact_id':result['artifact_id']},self.profile,self.limits,provider=provider)

    def test_repair_freezes_accepted_candidates_and_candidate_set(self):
        aid,_,_,c=self.publish(self.by_name['partial'])
        edited=deepcopy(c['input']['extraction']);edited['claims'][0]['text']='Changed accepted text'
        provider=OfflineProvider([self.response(edited)])
        result=self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual((result['status'],result['reason']),('failed','repair_changed_accepted'))
        # Different config still cannot make a second call on this B3 attempt.
        changed=self.profile.model_copy(update={'max_tokens':4000})
        other=OfflineProvider([self.response(c['input']['extraction'])])
        result=self.service.repair_input({'artifact_id':aid},changed,self.limits,provider=other,heartbeat=NoHeartbeat)
        self.assertEqual(result['reason'],'repair_already_attempted');self.assertEqual(other.requests,[])

    def test_repair_from_failed_b3_json_and_ineligible_finish(self):
        aid,outcome,owner,c=self.publish(content='{')
        self.assertIsNone(aid);self.assertEqual(outcome.status,'failed')
        provider=OfflineProvider([self.response(c['input']['extraction'])])
        repaired=self.service.repair_input({'call_id':outcome.call_id},self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(repaired['status'],'succeeded')
        # Reject a length response regardless of any valid JSON prefix.
        aid,outcome,owner,c=self.publish(self.by_name['growth'],content='{}')
        call=CallLedger(self.store).get(outcome.call_id);call['response']['body']['choices'][0]['finish_reason']='length'
        with patch('llm.events.extraction.budget.CallLedger.get',return_value=call):
            self.error('invalid_repair_input',self.service.repair_input,{'call_id':outcome.call_id},self.profile,self.limits,provider=provider)

    def test_repair_timeout_manual_retry_does_not_issue_second_call(self):
        aid,_,_,c=self.publish(self.by_name['uncovered'])
        provider=OfflineProvider([ProviderFailure('provider_timeout','Uncertain cost')])
        outcome=self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(outcome['status'],'failed');self.assertEqual(len(provider.requests),1)
        self.store.retry_job(outcome['job_id'])
        second=self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(second['reason'],'repair_already_attempted');self.assertEqual(len(provider.requests),1)
        self.assertEqual(CallLedger(self.store).get(outcome['call_id'])['state'],'unknown')

    def test_repair_parallel_different_profiles_reserve_once(self):
        aid,_,_,c=self.publish(self.by_name['uncovered'])
        providers=[OfflineProvider([self.response(c['input']['extraction'])]) for _ in range(2)]
        profiles=[self.profile,self.profile.model_copy(update={'max_tokens':4000})]
        def run(i):return NormalizationService(EventStore(self.store.path,clock=self.clock)).repair_input(
            {'artifact_id':aid},profiles[i],self.limits,provider=providers[i],heartbeat=NoHeartbeat)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,range(2)))
        self.assertEqual(sum(len(p.requests) for p in providers),1)
        self.assertIn('repair_already_attempted',[r.get('reason') for r in results])

    def test_repair_preserves_cost_after_revision_and_live_gate(self):
        aid,_,_,c=self.publish(self.by_name['uncovered']);outer=self
        class ChangingProvider(OfflineProvider):
            def complete(self,request):
                a=deepcopy(c['article']['article']);a['text']+=' Edit'
                outer.store.import_batch(json.dumps([a]).encode())
                return outer.response(c['input']['extraction'])
        result=self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=ChangingProvider([]),heartbeat=NoHeartbeat)
        self.assertEqual(result['status'],'superseded');self.assertEqual(CallLedger(self.store).get(result['call_id'])['state'],'settled')
        aid,_,_,c=self.publish(self.by_name['ambiguous_direction'])
        self.error('live_calls_disabled',self.service.repair_input,{'artifact_id':aid},ProviderProfile(),ExecutionLimits())

    def test_cli_normalization_schema_and_diagnostic_show(self):
        aid,_,_,_=self.publish()
        command=[sys.executable,'-m','llm.events.normalization.cli','--db',str(self.store.path)]
        result=subprocess.run(command+['normalize','--artifact-id',aid],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        artifact=json.loads(result.stdout)['artifact_id']
        shown=subprocess.run(command+['show',artifact],capture_output=True,text=True)
        self.assertEqual(shown.returncode,0);self.assertEqual(json.loads(shown.stdout)['payload']['schema_version'],'b4.1')
        checked=subprocess.run(command+['schema','--check'],capture_output=True,text=True)
        self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)

    def test_additional_numeric_guards_and_units(self):
        self.error('reversed_range',self.numeric,'Доход от 190 до 185 рублей.','190–185','рублей')
        self.error('unit_mismatch',self.numeric,'Выручка 10 долларов, а затраты 20 рублей.','10','рублей')
        self.error('scale_not_quoted',self.numeric,'Доход 1,31 рубля, активы 5 млрд рублей.','1,31','млрд рублей')
        self.error('unsupported_unit',self.numeric,'Показатель 12 фумов.','12','фумов')
        self.assertEqual(self.numeric('Пиковая мощность — 2,96 Тбит/с.','2,96','Тбит/с'),('2960000000000','bit_per_second'))
        self.assertEqual(self.numeric('Курс на 3,75 копейки выше.','3,75','копейки',kind='absolute_change')[0],'0.0375')

    def test_date_warnings_and_metadata_trace_are_not_erased(self):
        r=self.normalized('past_september');payload=r.model_dump(mode='json')
        self.assertEqual(r.quality,'partial');self.assertIn('/article/published_at',r.transformations[-1].metadata_refs)
        payload['diagnostics']=[];payload['quality']='complete'
        self.error('missing_date_assumption',validate_normalized_result,payload,self.by_name['past_september']['article'])
        r=self.normalized('percent');payload=r.model_dump(mode='json');payload['transformations'][0]['evidence_ids']=['missing']
        self.error('invalid_trace_reference',validate_normalized_result,payload,self.by_name['percent']['article'])

    def test_months_and_points_do_not_change_forecast_modality(self):
        c=deepcopy(self.by_name['future_september']);r=normalize_extraction(c['input'],c['article'])
        self.assertEqual(r.accepted[0].claim.modality,'forecast')
        self.assertIsNone(r.accepted[0].claim.stage)
        self.assertEqual(normalize_period('второе полугодие 2026 года',None).period['start'],'2026-07-01')
        self.assertIsNone(normalize_period('полугодие 2026 года',None).period['start'])

    def test_multiple_sentence_support_and_orphan_evidence(self):
        c=deepcopy(self.by_name['percent']);text=c['article']['article']['text']
        c['article']['article']['text']='Контекст. '+text
        from llm.events.contracts.models import ArticleInput
        c['input']['article_sha256']=article_sha256(ArticleInput.model_validate(c['article']['article']))
        ext=c['input']['extraction'];ext['evidence'].append({'evidence_id':'e2','field':'text','quote':'Контекст.'})
        ext['claims'][0]['evidence_ids'].append('e2');ext['claims'][0]['field_evidence']['/text'].append('e2')
        r=normalize_extraction(c['input'],c['article']);self.assertEqual(len(r.evidence),2)
        self.assertNotEqual(r.evidence[0].start,r.evidence[1].start)
        # Broken unused evidence still leaves a diagnostic, but not a phantom claim.
        ext['evidence'].append({'evidence_id':'e3','field':'text','quote':'Missing unused fragment'})
        r=normalize_extraction(c['input'],c['article']);self.assertEqual(len(r.accepted),1);self.assertEqual(r.quality,'partial')

    def test_cached_repair_reuses_call_and_rechecks_normalization(self):
        aid,_,_,c=self.publish(self.by_name['uncovered']);fixed=deepcopy(c['input']['extraction'])
        fixed['claims'][0]['field_evidence']['/modality']=['e1'];provider=OfflineProvider([self.response(fixed)])
        first=self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        second=self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(second['status'],'cached');self.assertEqual(second['normalization']['status'],'cached')
        self.assertEqual(first['artifact_id'],second['artifact_id']);self.assertEqual(len(provider.requests),1)

    def test_repair_warning_only_is_not_a_paid_ambiguity_resolver(self):
        aid,_,_,c=self.publish(self.by_name['past_september']);provider=OfflineProvider([])
        self.error('repair_not_needed',self.service.repair_input,{'artifact_id':aid},self.profile,self.limits,provider=provider)
        self.assertEqual(provider.requests,[])

    def test_repair_changed_candidate_set_is_rejected(self):
        aid,_,_,c=self.publish(self.by_name['uncovered']);fixed=deepcopy(c['input']['extraction']);fixed['claims']=[]
        result=self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=OfflineProvider([self.response(fixed)]),heartbeat=NoHeartbeat)
        self.assertEqual(result['reason'],'repair_changed_claim_set')

    def test_repair_crash_after_reserve_consumes_slot_without_retry(self):
        aid,_,_,c=self.publish(self.by_name['uncovered']);provider=OfflineProvider([KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        calls=[c for c in CallLedger(self.store).list_calls() if c['state']=='reserved']
        self.assertEqual(len(calls),1)
        self.clock.value+=901;self.store.requeue_expired()
        again=self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(again['reason'],'repair_already_attempted');self.assertEqual(len(provider.requests),1)
        self.assertEqual(CallLedger(self.store).get(calls[0]['call_id'])['state'],'reserved')

    def test_repair_cannot_bypass_budget_and_retry_after_quota_failure_can_reserve(self):
        aid,_,_,c=self.publish(self.by_name['uncovered']);provider=OfflineProvider([self.response(c['input']['extraction'])])
        expensive=self.limits.model_copy(update={'input_rub_per_million':'1000000'})
        first=self.service.repair_input({'artifact_id':aid},self.profile,expensive,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(first['reason'],'budget_exhausted');self.assertEqual(provider.requests,[])
        self.store.retry_job(first['job_id'])
        second=self.service.repair_input({'artifact_id':aid},self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(len(provider.requests),1)

    def test_check_fixture_cli_and_current_database_version(self):
        result=subprocess.run([sys.executable,'-m','llm.events.normalization.cli','check'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['cases'],22)
        conn=sqlite3.connect(self.store.path)
        try:self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],SCHEMA_VERSION)
        finally:conn.close()

    def test_level_cannot_invent_minus_and_calendar_ranges_preserve_both_bounds(self):
        self.error('number_sign_mismatch',self.numeric,'Прибыль 5 рублей.','-5','рублей')
        self.error('number_sign_mismatch',self.numeric,'Прибыль −5 рублей.','5','рублей')
        self.assertEqual(self.numeric('Прибыль −5 рублей.','−5','рублей')[0],'-5')
        for text,start,end in [('с сентября 2025 года по январь 2026 года','2025-09-01','2026-01-31'),
            ('с 1 сентября 2025 года по 10 января 2026 года','2025-09-01','2026-01-10'),
            ('2025-09-01 — 2026-01-10','2025-09-01','2026-01-10')]:
            r=normalize_period(text,None)
            self.assertEqual((r.period['start'],r.period['end']),(start,end))

    def test_unsupported_period_never_expands_into_a_calendar_year(self):
        for text in ['за первые пять дней 2026 года','23.10.25','2026-1-2']:
            r=normalize_period(text,'2026-10-20T12:00:00+03:00')
            self.assertIsNone(r.period['start']);self.assertTrue(r.warnings)
        r=normalize_period('с начала года','2026-10-20T12:00:00+03:00')
        self.assertEqual(r.period['start'],'2026-01-01');self.assertIsNone(r.period['end'])
        self.assertIn('observation_cutoff_unknown',r.warnings)
        r=normalize_period('в этом году','2026-10-20T12:00:00+03:00',context='По сравнению с итогами 2025 года')
        self.assertEqual(r.period['start'],'2026-01-01')
