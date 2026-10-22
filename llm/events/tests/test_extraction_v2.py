"""Offline v2 packet extraction, field evidence, strict selection and A/B lineage."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import json
import unittest
from jsonschema import Draft202012Validator
from llm.events.contracts.models import ArticleInput, ArticleRecord
from llm.events.extraction.models import ProviderProfile, ExecutionLimits
from llm.events.extraction.provider import OfflineProvider, ProviderResponse
from llm.events.storage.store import EventStore, StorageError, processing_hash
from llm.events.tests.test_extraction import NoHeartbeat
from llm.events.extraction import staged as core, staged_models as models
from llm.events.extraction.service import ExtractionService, StageRunner, ExperimentCounter
from llm.events.extraction.evaluation import compare_variants
from llm.events.extraction.cli import schemas
from llm.events.normalization.service import NormalizationService
from llm.events.contracts.validation import validate_grounding, article_sha256

ARTICLE={'article_id':'synthetic:grounded-v2','source':'synthetic','url':None,'title':'Результаты компании',
 'published_at':'2026-10-20T12:00:00+03:00','language':'ru',
 'text':'АО Север сообщило: выручка за 2026 год составила 10 млн рублей.\n\nЭксперт ожидает ставку 13,5% в 2027 году.',
 'content_kind':'original','is_full_original_text':True,'is_synthetic':True}
QUOTE=ARTICLE['text'].split('\n\n')[0]

def bound(value,quote=QUOTE,fragment='P1'):
 return {'value':value,'evidence':[{'fragment_id':fragment,'quote':quote}]}

def observation(mid='d1'):
 return {'mention_id':mid,'template':'observation','text':bound('Выручка АО Север за 2026 год составила 10 млн рублей.'),
  'event_type':bound('company_results'),'modality':bound('asserted'),'polarity':bound('affirmed'),
  'subject':bound('АО Север'),'author':bound('АО Север'),'object':bound('выручка'),
  'period':bound('2026 год'),'measurements':[{'metric':bound('выручка'),'value_text':bound('10'),
   'unit_text':bound('млн рублей'),'kind':bound('level'),'qualifier':bound('exact'),'value_role':bound('reported')}]}

DISCOVERY={'mentions':[{'primary_fragment_id':'P1','context_fragment_ids':[],'label':'выручка',
 'trigger':{'fragment_id':'P1','quote':'выручка за 2026 год'},'event_type':'company_results','possible_templates':['observation']}],'limitations':[]}

def response(data,finish='stop'):
 return ProviderResponse({'choices':[{'finish_reason':finish,'message':{'content':json.dumps(data,ensure_ascii=False)}}],
                         'usage':{'prompt_tokens':100,'completion_tokens':100}})

class V2Tests(unittest.TestCase):
 def setUp(self):
  self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.store=EventStore(Path(self.temp.name)/'state.sqlite3')
  self.record=self.store.get_article(self.store.import_batch(json.dumps([ARTICLE]).encode()).items[0].source_id)
  self.profile=ProviderProfile(execution_mode='offline',model='offline:scripted',limits_confirmed=True,
    max_input_tokens=30000,max_output_tokens=4096,context_window_tokens=34096)
  self.limits=ExecutionLimits(tariffs_confirmed=True,input_rub_per_million='0',output_rub_per_million='0',daily_quota_rub='1')

 def service(self,responses):
  return ExtractionService(self.store,OfflineProvider(responses),self.profile,limits=self.limits,heartbeat=NoHeartbeat)

 def discovery(self,data=None):
  worker=StageRunner(self.store,OfflineProvider([]),stage='discover')
  return core.parse_discovery(json.dumps(data or DISCOVERY),self.record,worker.build_spec(self.profile))

 def plan(self,data=None,profile=None):
  d=self.discovery(data);p=profile or self.profile
  return d,core.plan_extraction(self.record,d,'synthetic-artifact',p,'synthetic-config')

 def prepare_a(self):
  self.svc=self.service([response(DISCOVERY),response({'records':[observation()],'excluded_mentions':[],'limitations':[]})])
  discovery=self.svc.discover(self.record.source_id)
  self.plan_saved=self.svc.plan_extraction(discovery['artifact_id'])
  batches=self.svc.run_extraction(self.plan_saved['artifact_id'],max_jobs=1)
  self.batch_ids=[b['artifact_id'] for b in batches if b.get('artifact_id')]
  self.assembly=self.svc.assemble_variant(self.plan_saved['artifact_id'],self.batch_ids)
  self.norm=self.svc.normalize_variant(self.assembly)
  self.assertEqual(self.norm['status'],'succeeded')
  return self.store.get_artifact(self.norm['artifact_id']).payload

 def verdict(self,normalized,*,bad=None):
  verdicts=[]
  for a in normalized['accepted']:
   fields=[{'path':p,'status':'supported','evidence':[{'fragment_id':'P1','quote':QUOTE}]} for p in core.required_paths(a)]
   if bad=='missing':fields.pop()
   if bad=='contradicted':fields[0]['status']='contradicted'
   if bad=='quote':fields[0]['evidence'][0]['quote']='absent'
   if bad=='duplicate':fields.append(deepcopy(fields[0]))
   if bad=='extra':fields.append({'path':'/invented','status':'supported','evidence':[]})
   verdicts.append({'candidate_id':a['candidate_id'],'fields':fields})
  return {'verdicts':verdicts}

 def test_offline_a_b_publication_cache_and_no_semantic_relabel(self):
  normalized=self.prepare_a()
  self.assertEqual(normalized['accepted'][0]['claim']['measurements'][0]['value'],'10000000')
  verifier=self.service([response(self.verdict(normalized))])
  reviews=verifier.verify_extraction(self.norm['artifact_id'],max_jobs=1)
  b=verifier.assemble_variant(self.plan_saved['artifact_id'],self.batch_ids,variant='B',
    normalized_artifact_id=self.norm['artifact_id'],verification_artifact_ids=[reviews[0]['artifact_id']])
  normalized_b=verifier.normalize_variant(b)
  self.assertEqual(normalized_b['status'],'succeeded')
  payload_b=self.store.get_artifact(normalized_b['artifact_id']).payload
  self.assertEqual(payload_b['semantic_support'],'not_assessed')
  raw_a=self.store.get_artifact(self.assembly['artifact_id']).payload['extraction']['claims']
  raw_b=self.store.get_artifact(b['artifact_id']).payload['extraction']['claims']
  self.assertEqual(raw_a,raw_b)
  a_pub=self.svc.publish_variant('A',[self.norm['artifact_id']],experiment_id='test')
  b_pub=verifier.publish_variant('B',[normalized_b['artifact_id']],experiment_id='test')
  self.assertNotEqual(a_pub['catalog_id'],b_pub['catalog_id'])
  for pub in (a_pub,b_pub):
   for bundle in pub['bundles']:validate_grounding(bundle,[self.record])
  cached=verifier.verify_extraction(self.norm['artifact_id'],max_jobs=1)
  self.assertEqual(cached[0]['status'],'cached');self.assertEqual(len(verifier.provider.requests),1)
  with self.assertRaises(StorageError):self.svc.publish_variant('B',[self.norm['artifact_id']],experiment_id='bad')

 def test_partial_assembly_marks_b4_and_b6(self):
  d=deepcopy(DISCOVERY);d['mentions']*=4
  svc=self.service([response(d),response({'records':[observation('d1')],'excluded_mentions':[],'limitations':[]})])
  first=svc.discover(self.record.source_id);plan=svc.plan_extraction(first['artifact_id'])
  self.assertEqual([len(b['mention_ids']) for b in plan['plan']['batches']],[3,1])
  batches=svc.run_extraction(plan['artifact_id'],max_jobs=1)
  self.assertEqual(batches[1]['reason'],'run_limit')
  assembled=svc.assemble_variant(plan['artifact_id'],[batches[0]['artifact_id']])
  self.assertTrue(assembled['incomplete'])
  result=svc.normalize_variant(assembled);payload=self.store.get_artifact(result['artifact_id']).payload
  self.assertIn('input_extraction_incomplete',[d['issue']['code'] for d in payload['diagnostics']])
  pub=svc.publish_variant('A',[result['artifact_id']],experiment_id='partial')
  self.assertTrue(all(b['event']['quality']=='partial' for b in pub['bundles']))
  # Resume skips the completed first job and can dispatch the next one with max_jobs=1.
  resumed=self.service([response({'records':[observation('d4')],'excluded_mentions':[],'limitations':[]})])
  results=resumed.run_extraction(plan['artifact_id'],max_jobs=1)
  self.assertEqual(results[0]['status'],'cached');self.assertEqual(results[1]['status'],'succeeded')

 def test_bad_record_preserves_good_record_and_marks_coverage(self):
  d=deepcopy(DISCOVERY);d['mentions']*=2;discovery,plan=self.plan(d)
  bad=observation('d2');bad['measurements'][0].pop('unit_text')
  result=core.compile_batch(json.dumps({'records':[observation(),bad],'excluded_mentions':[],'limitations':[]}),
    self.record,discovery,plan.batches[0])
  self.assertEqual(len(result['extraction']['claims']),1)
  self.assertEqual([c['status'] for c in result['coverage']],['processed','rejected'])
  self.assertTrue(result['bindings']);self.assertTrue(result['issues'])

 def test_verification_rejects_partial_unknown_duplicate_and_bad_quote(self):
  normalized=self.prepare_a();accepted=normalized['accepted']
  for bad in ('missing','contradicted','quote','duplicate','extra'):
   result=core.parse_verification(json.dumps(self.verdict(normalized,bad=bad)),self.record,self.norm['artifact_id'],accepted)
   self.assertFalse(result.selections[0].accepted,bad)
  unknown=deepcopy(accepted);unknown[0]['claim']['polarity']='unknown'
  data={'accepted':unknown};verdict=self.verdict(data)
  self.assertFalse(core.parse_verification(json.dumps(verdict),self.record,self.norm['artifact_id'],unknown).selections[0].accepted)
  # Missing all verification excludes whole claims, keeping immutable A.
  b=self.svc.assemble_variant(self.plan_saved['artifact_id'],self.batch_ids,variant='B',normalized_artifact_id=self.norm['artifact_id'])
  self.assertEqual(b['candidate_count'],0)
  self.assertEqual(len(self.store.get_artifact(self.assembly['artifact_id']).payload['extraction']['claims']),1)
  self.assertEqual(self.svc.normalize_variant(b)['status'],'failed')

 def test_quotes_unicode_ambiguity_and_wrong_context(self):
  record=self.record.model_copy(deep=True);record.article.text='😀 Север\u00a0Север.\n\nДругой текст.'
  span=core.locate({'fragment_id':'P1','quote':'😀 Север'},record)
  self.assertEqual(record.article.text[span['start']:span['end']],span['quote'])
  for ref in [{'fragment_id':'P1','quote':'Север'},{'fragment_id':'P1','quote':'нет'},{'fragment_id':'bad','quote':'Север'}]:
   with self.assertRaises(StorageError):core.locate(ref,record)
  # Same quote in distinct fragments is disambiguated locally and expanded for B3 compatibility.
  record.article.text='Первый Север.\n\nВторой Север.'
  span=core.locate({'fragment_id':'P2','quote':'Север'},record)
  e=core._unique_evidence(span,record)
  self.assertEqual(record.article.text.count(e['quote']),1)

 def test_explicit_table_headers_and_units_no_fuzzy_dedup(self):
  record=self.record.model_copy(deep=True)
  record.article.text='Поставка в тыс. тонн:\n\n| Страна | год |\n\n| А | 10 |\n\n| Б | 20 |'
  mention=models.MentionRecord(mention_id='d1',primary_fragment_id='P3',context_fragment_ids=[],label='А',
    trigger={'fragment_id':'P3','quote':'10'},event_type='market_indicator',possible_templates=['observation'])
  context=core.context_for(record,[mention]);ids={f['id'] for f in context}
  self.assertTrue({'P1','P2','P3'}<=ids)
  record.article.text='| А | 10 |\n\n| Б | 20 |'
  mention.primary_fragment_id='P2'
  with self.assertRaises(StorageError):core.context_for(record,[mention])

 def test_schemas_and_all_seven_templates(self):
  for schema in schemas().values():Draft202012Validator.check_schema(schema)
  for template in models.TEMPLATES:
   fact=observation();fact['template']=template
   if template!='observation':fact.pop('period')
   if template.endswith('forecast') or template=='forecast_revision':
    fact['modality']=bound('forecast');fact['horizon']=bound('2026 год')
   if template=='scenario_forecast':fact['scenario']={'kind':bound('baseline'),'text':bound('выручка')}
   if template=='forecast_revision':
    fact['measurements'][0]['value_role']=bound('before')
    fact['measurements'].append(deepcopy(fact['measurements'][0]));fact['measurements'][1]['value_role']=bound('after')
   if template=='decision':fact['stage']=bound('approved')
   models.TEMPLATE.validate_python(fact)
   Draft202012Validator(core.record_schema([template])).validate({'records':[fact],'excluded_mentions':[],'limitations':[]})

 def test_template_alternatives_and_unresolved_type(self):
  d=deepcopy(DISCOVERY);d['mentions'][0]['possible_templates']=['indicator_forecast','observation']
  discovery,plan=self.plan(d)
  result=core.compile_batch(json.dumps({'records':[observation()],'excluded_mentions':[],'limitations':[]}),self.record,discovery,plan.batches[0])
  self.assertEqual(result['coverage'][0]['status'],'processed')
  result=core.compile_batch(json.dumps({'records':[],'excluded_mentions':[{'mention_id':'d1','reason':'ambiguous','unresolved_type':True}],'limitations':[]}),self.record,discovery,plan.batches[0])
  self.assertEqual(result['coverage'][0]['status'],'unresolved')

 def test_size_split_and_individual_defer(self):
  discovery,plan=self.plan()
  tiny=self.profile.model_copy(update={'max_request_bytes':100})
  deferred=core.plan_extraction(self.record,discovery,'d',tiny,'c')
  self.assertEqual(deferred.batches[0].deferred_reason,'input_too_large')
  d=deepcopy(DISCOVERY);d['mentions']*=3;discovery,_=self.plan(d)
  one=plan.batches[0].request_bytes
  profile=self.profile.model_copy(update={'max_request_bytes':one+20})
  split=core.plan_extraction(self.record,discovery,'d',profile,'c')
  self.assertGreater(len(split.batches),1)

 def test_truncated_batch_not_retried_and_next_batch_runs(self):
  d=deepcopy(DISCOVERY);d['mentions']*=4
  svc=self.service([response(d),response({},finish='length'),response({'records':[observation('d4')],'excluded_mentions':[],'limitations':[]})])
  first=svc.discover(self.record.source_id);plan=svc.plan_extraction(first['artifact_id'])
  results=svc.run_extraction(plan['artifact_id'],max_jobs=2)
  self.assertEqual(results[0]['reason'],'output_limit_exceeded');self.assertEqual(results[1]['status'],'succeeded')
  again=svc.run_extraction(plan['artifact_id'],max_jobs=2)
  self.assertEqual(again[0]['status'],'failed');self.assertEqual(again[1]['status'],'cached')
  self.assertEqual(len(svc.provider.requests),3)

 def test_stale_source_and_audit_mismatch(self):
  self.prepare_a()
  with self.assertRaises(StorageError):NormalizationService(self.store).normalize_artifact(self.assembly['artifact_id'],input_audit_artifact_id=self.norm['artifact_id'])
  changed=deepcopy(ARTICLE);changed['title']='Changed'
  self.store.import_batch(json.dumps([changed]).encode())
  with self.assertRaises(StorageError):self.svc.verify_extraction(self.norm['artifact_id'],max_jobs=1)

 def test_persistent_quota_concurrent_reservations_and_probe_cap(self):
  path=Path(self.temp.name)/'quota.sqlite3';counter=ExperimentCounter(path,3)
  probe={'messages':[{'content':'FORMAT_PROBE_V2 check'}]}
  counter.reserve(probe);counter.reserve(probe)
  with self.assertRaises(StorageError):counter.reserve(probe)
  def reserve(_):
   try:return ExperimentCounter(path,3).reserve({'messages':[]})
   except StorageError:return None
  with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(x is not None for x in pool.map(reserve,range(2))),1)
  with self.assertRaises(StorageError):ExperimentCounter(path,4)
  counter.freeze_profile(self.profile)
  with self.assertRaises(StorageError):counter.freeze_profile(self.profile.model_copy(update={'max_tokens':1024}))

 def test_compare_requires_human_alignment_and_checks_frozen_source(self):
  normalized=self.prepare_a();record=self.record
  gold={'schema_version':'extraction-gold.v1','status':'single_annotator_draft','articles':[{'article_id':ARTICLE['article_id'],'article_sha256':article_sha256(record.article)}],
   'cases':[{'article_id':ARTICLE['article_id'],'gold_id':'g1','evidence':{'field':'text','quote':QUOTE,'start':0,'end':len(QUOTE)},'fields':{'/measurements':[{'value':'10000000'}]}}]}
  result=compare_variants(self.store,[self.norm['artifact_id']],[],gold=gold)
  self.assertEqual(result['status'],'awaiting_human_alignment');self.assertIsNone(result['variants']['A']['metrics']['field_accuracy'])
  cid=normalized['accepted'][0]['candidate_id']
  result=compare_variants(self.store,[self.norm['artifact_id']],[],gold=gold,alignments=[{'article_id':ARTICLE['article_id'],'candidate_id':cid,'gold_id':'g1','candidate_correct':True}])
  self.assertEqual(result['variants']['A']['metrics']['field_accuracy'],1)
  self.assertEqual(result['verification_confusion']['not_verified'],1)
  gold['articles'][0]['article_sha256']='0'*64
  with self.assertRaises(StorageError):compare_variants(self.store,[self.norm['artifact_id']],[],gold=gold)


class ExperimentTests(unittest.TestCase):
 setUp=V2Tests.setUp
 def test_whole_experiment_cli_pipeline_without_network(self):
  from llm.events.extraction.cli import run_experiment
  from llm.events.extraction import prompts
  from llm.events.extraction.evaluation import export_comparison
  calls=[]
  def complete(request):
   calls.append(request);prompt=request['messages'][0]['content']
   if prompt.startswith(prompts.DISCOVERY):return response(DISCOVERY)
   if prompt.startswith(prompts.EXTRACTION):return response({'records':[observation()],'excluded_mentions':[],'limitations':[]})
   payload=json.loads(request['messages'][1]['content'].split('\n',1)[1])
   return response({'verdicts':[{'candidate_id':r['candidate_id'],'fields':[{'path':p,'status':'supported','evidence':[{'fragment_id':'P1','quote':QUOTE}]} for p in r['required_paths']]} for r in payload['review']]})
  provider=SimpleNamespace(is_live=False,complete=complete)
  service=ExtractionService(self.store,provider,self.profile,limits=self.limits,heartbeat=NoHeartbeat)
  article_path=Path(self.temp.name)/'articles.json';article_path.write_text(json.dumps([ARTICLE]))
  report,journal,bundles=run_experiment(service,article_path,max_jobs=10,experiment_id='synthetic-e2e')
  self.assertEqual(len(calls),3);self.assertEqual(report['requests'],3)
  self.assertEqual(set(bundles),{'A','B'})
  self.assertEqual(report['variants']['B']['articles'][0]['accepted'],1)
  export_comparison(Path(self.temp.name)/'export',report,calls=journal,bundles=bundles,inputs=[ARTICLE])
  again,_,_=run_experiment(service,article_path,max_jobs=10,experiment_id='synthetic-e2e')
  self.assertEqual(len(calls),3)
  self.assertEqual(again['rows'][0]['discovery']['status'],'cached')
  gold={'schema_version':'extraction-gold.v1','status':'single_annotator_draft','articles':[],'cases':[]}
  with self.assertRaises(StorageError):run_experiment(service,article_path,max_jobs=10,gold=gold,experiment_id='wrong-gold')
  self.assertEqual(len(calls),3)

 def test_probe_cap_and_frozen_profile_with_fake_provider(self):
  from llm.events.extraction.service import FormatProbeRunner
  from llm.events.extraction.cli import select_profile
  calls=[]
  profile=self.profile.model_copy(update={'execution_mode':'live','model':'deepseek/deepseek-v4.1-flash','response_format':'json_schema'})
  def complete(request):calls.append(request);return response({'ok':True})
  provider=SimpleNamespace(is_live=True,profile=profile,check_credentials=lambda:None,complete=complete)
  counter=ExperimentCounter(Path(self.temp.name)/'probes.sqlite3',60)
  service=ExtractionService(self.store,provider,profile,counter=counter)
  probe=FormatProbeRunner(self.store,provider)
  first=service._call(self.record,probe);self.assertEqual(first['status'],'succeeded')
  chosen=select_profile(self.store,profile,[first['artifact_id']])
  self.assertEqual(chosen.response_format,'json_schema');self.assertTrue(chosen.format_verified)
  counter.freeze_profile(chosen)
  with self.assertRaises(StorageError):counter.reserve({'messages':[{'content':'FORMAT_PROBE_V2 new'}]})
  self.assertEqual(len(calls),1)

class BoundaryTests(unittest.TestCase):
 setUp=V2Tests.setUp

 def test_revision_pair_compiles_together_and_normalizes_without_float(self):
  from llm.events.extraction.models import ExtractionResult
  from llm.events.normalization.core import normalize_extraction
  from llm.events.storage.models import ProcessingSpec
  record=self.record.model_copy(deep=True)
  q='Эксперт пересмотрел прогноз инфляции на 2027 год с 6% до 5%.'
  record.article.text=q
  def b(v):return bound(v,q)
  data=deepcopy(DISCOVERY);data['mentions'][0].update(label='пересмотр прогноза',event_type='market_indicator',
   possible_templates=['forecast_revision'],trigger={'fragment_id':'P1','quote':'пересмотрел прогноз'})
  worker=StageRunner(self.store,OfflineProvider([]),stage='discover')
  discovery=core.parse_discovery(json.dumps(data),record,worker.build_spec(self.profile))
  plan=core.plan_extraction(record,discovery,'d',self.profile,'c')
  measurement=lambda value,role:{'metric':b('инфляции'),'value_text':b(value),'unit_text':b('%'),
   'kind':b('level'),'qualifier':b('exact'),'value_role':b(role)}
  fact={'mention_id':'d1','template':'forecast_revision','text':b(q),'event_type':b('market_indicator'),
   'modality':b('forecast'),'polarity':b('affirmed'),'author':b('Эксперт'),'horizon':b('2027 год'),
   'measurements':[measurement('6','before'),measurement('5','after')]}
  result=core.compile_batch(json.dumps({'records':[fact],'excluded_mentions':[],'limitations':[]}),record,discovery,plan.batches[0])
  self.assertEqual(len(result['extraction']['claims']),1)
  raw=ExtractionResult(source_id=record.source_id,article_sha256=article_sha256(record.article),processing_spec_hash='synthetic',extraction=result['extraction'])
  normalized=normalize_extraction(raw,record)
  self.assertEqual(len(normalized.accepted),1)
  self.assertEqual([(m.value,m.value_role) for m in normalized.accepted[0].claim.measurements],[('6','before'),('5','after')])
  self.assertIsNotNone(normalized.accepted[0].claim.time.forecast_horizon)
  self.assertIsNone(normalized.accepted[0].claim.time.observation_period)

 def test_credit_exhaustion_stops_without_second_call_or_quota_reset(self):
  calls=[]
  profile=self.profile.model_copy(update={'execution_mode':'live','model':'deepseek/deepseek-v4.1-flash'})
  def complete(request):calls.append(request);return response(DISCOVERY)
  provider=SimpleNamespace(is_live=True,profile=profile,check_credentials=lambda:None,complete=complete)
  counter=ExperimentCounter(Path(self.temp.name)/'one.sqlite3',1)
  service=ExtractionService(self.store,provider,profile,counter=counter)
  discovered=service.discover(self.record.source_id);plan=service.plan_extraction(discovered['artifact_id'])
  out=service.run_extraction(plan['artifact_id'],max_jobs=1)
  self.assertEqual(out[0]['reason'],'pilot_limit_exhausted');self.assertTrue(out[0]['stop'])
  self.assertEqual(len(calls),1);self.assertEqual(len(ExperimentCounter(counter.path,1).summary()),1)

 def test_standard_live_execution_keeps_original_budget_gate(self):
  profile=self.profile.model_copy(update={'execution_mode':'live','model':'deepseek/deepseek-v4.1-flash'})
  calls=[]
  provider=SimpleNamespace(is_live=True,profile=profile,check_credentials=lambda:None,
    complete=lambda request:calls.append(request))
  service=ExtractionService(self.store,provider,profile,limits=ExecutionLimits())
  with self.assertRaises(StorageError):service.discover(self.record.source_id)
  self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
