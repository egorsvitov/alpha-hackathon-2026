from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from llm.events.catalog.models import CatalogConfig, ClaimContext, PairDecision, CanonicalEventRecord, CatalogSnapshot, ReviewRequest
from llm.events.catalog.service import CatalogService, implementation_hash
from llm.events.catalog.checks import check_fixtures, evaluate_gold
from llm.events.catalog.search import BM25CandidateSearch, pair_key
from llm.events.catalog.grouping import cluster_claims
from llm.events.catalog.matching import describe_identity, compare_claims, semantic_claim, related_stages
from llm.events.fixtures.make_catalog import context,TYPES
from llm.events.fixtures.make_normalization import case,measurement
from llm.events.extraction.protocol import build_processing_spec
from llm.events.extraction.models import ProviderProfile, ExecutionLimits
from llm.events.extraction.runner import ExtractionRunner
from llm.events.extraction.provider import OfflineProvider, ProviderResponse, ProviderFailure
from llm.events.normalization.service import NormalizationService
from llm.events.normalization.models import NormalizationConfig, Transformation
from llm.events.catalog.pair_check import parse_recommendation
from llm.events.storage.store import EventStore, StorageError
from llm.events.storage.schema import V2_DDL, V2_TABLES, SCHEMA_VERSION
from llm.events.storage.migration import migrate_v2_to_v3, migrate_v3_to_v4
from llm.events.contracts.validation import ContractValidationError, validate_claim_collection

ROOT=Path(__file__).resolve().parents[3]

class Clock:
    def __init__(self):self.value=1792501200.0
    def __call__(self):return self.value

class NoHeartbeat:
    lost=False
    def __init__(self,*_):pass
    def __enter__(self):return self
    def __exit__(self,*_):pass


def ctx(name,*args,**kwargs):return ClaimContext.model_validate(context(name,*args,**kwargs))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.clock=Clock();self.db=Path(self.temp.name)/'state.sqlite3'
        self.store=EventStore(self.db,clock=self.clock);self.service=CatalogService(self.store)
        self.profile=ProviderProfile(candidate_format='b3.1',execution_mode='offline',model='offline:scripted',limits_confirmed=True,
            max_input_tokens=30000,max_output_tokens=4096,context_window_tokens=34096)
        self.limits=ExecutionLimits(tariffs_confirmed=True,input_rub_per_million='1',output_rub_per_million='2',daily_quota_rub='1')

    def error(self,code,fn,*args,**kwargs):
        with self.assertRaises(ContractValidationError) as caught:fn(*args,**kwargs)
        self.assertIn(code,[i.code for i in caught.exception.issues])
        return caught.exception

    def publish(self,aid='a',value='14',day='2026-10-23',*,empty=False,raw=None,config=None):
        text=f'Банк России утвердил решение {day}: ключевая ставка составляет {value}% в России.'
        raw=deepcopy(raw) if raw else case(aid,text,[measurement(value,'%')],entity='Банк России',expected={})
        raw['article']['article']['article_id']=aid
        c=raw['input']['extraction']['claims'][0]
        c.update(stage='approved',action='keep',object='policy_rate',geographies=['RU'])
        c['time']['occurred_at']={'text':day}
        for key in ('stage','action','object','geographies','time'):c['field_evidence']['/'+key]=['e1']
        if empty:raw['input']['extraction'].update(claims=[],evidence=[])
        sid=self.store.import_batch(json.dumps([raw['article']['article']],ensure_ascii=False).encode()).items[0].source_id
        spec=build_processing_spec(self.profile);job=self.store.enqueue(sid,spec)
        if job.state!='succeeded':
            provider=OfflineProvider([self.response(raw['input']['extraction'])])
            outcome=ExtractionRunner(self.store,provider,heartbeat=NoHeartbeat).run_next(spec,self.limits,source_id=sid)
            self.assertEqual(outcome.status,'succeeded')
        artifact=self.store.get_job(job.job_id).result_artifact_id
        result=NormalizationService(self.store).normalize_artifact(artifact,config)
        self.assertIn(result['status'],('succeeded','cached'),result)
        return result['artifact_id']

    def response(self,data,finish='stop',usage=True,status=200):
        body={'choices':[{'finish_reason':finish,'message':{'content':json.dumps(data,ensure_ascii=False)}}]}
        if usage:body['usage']={'prompt_tokens':100,'completion_tokens':20,'completion_tokens_details':{'reasoning_tokens':19}}
        return ProviderResponse(body,status)

    def initialize(self,*artifacts):
        self.service.create_catalog()
        return self.service.apply_artifacts(list(artifacts))

    def test_fixtures_and_contracts(self):
        report=check_fixtures(ROOT/'llm/events/fixtures/catalog.b5.1.json');self.assertEqual(report['cases'],33)
        for model,name in [(CatalogSnapshot,'event-catalog'),(__import__('llm.events.catalog.models',fromlist=['IdentityDescriptor']).IdentityDescriptor,'event-identity'),
                           (__import__('llm.events.catalog.models',fromlist=['PairCheckResult']).PairCheckResult,'pair-check')]:
            schema=json.loads((ROOT/f'docs/contracts/{name}.b5.1.schema.json').read_text());schema.pop('$schema')
            self.assertEqual(schema,model.model_json_schema())
        with self.assertRaises(ValidationError):CatalogConfig(max_claims='10')
        with self.assertRaises(ValidationError):CatalogConfig(extra=1)
        with self.assertRaises(ValidationError):CatalogConfig(max_comparisons=0)

    def test_all_categories_explicit_difference(self):
        for category in TYPES:
            a,b=ctx('a',category),ctx('b',category)
            b.claim.object='another_specific_subject'
            self.assertEqual(compare_claims(a,b).identity,'different_event',category)

    def test_retrieval_top_k_does_not_cap_structural_matches(self):
        data=[ctx(f'c{i:02}',value=str(i+1)) for i in range(24)]
        found=BM25CandidateSearch().search(data,CatalogConfig(top_k=1))
        self.assertEqual(len(found),24*23//2)
        self.assertTrue(all('structure' in r['methods'] for r in found.values()))
        self.error('catalog_comparison_limit',BM25CandidateSearch().search,data,CatalogConfig(max_comparisons=10))

    def test_duplicate_decimal_order_and_additional_measurement(self):
        a,b=ctx('a',value='14.00'),ctx('b',value='14')
        self.assertEqual(compare_claims(a,b).content,'duplicate_claim')
        extra=b.claim.measurements[0].model_copy(update={'measurement_id':'extra','kind':'relative_change','value':'1'})
        a.claim.measurements.append(extra);b.claim.measurements.insert(0,extra)
        self.assertEqual(compare_claims(a,b).content,'duplicate_claim')
        b.claim.measurements.pop(0)
        self.assertEqual(compare_claims(a,b).content,'complements')
        huge='1'+'0'*70
        a,b=ctx('a',value=huge),ctx('b',value=huge[:-1]+'1')
        self.assertEqual(compare_claims(a,b).content,'conflicts_with')

    def test_ranges_and_unknown_conditions(self):
        a,b=ctx('a'),ctx('b')
        from llm.events.contracts.models import ValueRange
        a.claim.measurements[0].value=ValueRange(min='10',max='12')
        b.claim.measurements[0].value=ValueRange(min='13',max='15')
        self.assertEqual(compare_claims(a,b).content,'conflicts_with')
        b.claim.measurements[0].value=ValueRange(min='11',max='15')
        self.assertEqual(compare_claims(a,b).content,'complements')
        b.claim.measurements[0].qualifier='approximate'
        self.assertEqual(compare_claims(a,b).content,'complements')
        b.claim.measurements[0].unit='percentage_point'
        self.assertNotEqual(compare_claims(a,b).content,'conflicts_with')

    def test_assumed_year_and_basis_coordinates(self):
        a,b=ctx('a'),ctx('b')
        a.transformations.append(Transformation(candidate_id='a',path='/time/occurred_at/text',rule='nearest_period_year',
            input_value='23 октября',output_value={},evidence_ids=['a:e'],metadata_refs=['published_at']))
        self.assertEqual(compare_claims(a,b).identity,'uncertain')
        a=ctx('a',forecast=True)
        basis=next(b for b in describe_identity(a).bases if b.feature=='release')
        evidence=a.evidence[0]
        self.assertEqual(evidence.quote[basis.start:basis.end],'Прогноз №F1')

    def test_no_transitive_merge_and_permutation(self):
        a,b,c=ctx('a'),ctx('b'),ctx('c',day='2026-09-18')
        # Unknown occurrence cannot bridge incompatible meetings.
        b.claim.time.occurred_at=None
        data={x.claim.claim_id:x for x in (a,b,c)}
        one=cluster_claims(data,CatalogConfig())[0]
        two=cluster_claims(dict(reversed(list(data.items()))),CatalogConfig())[0]
        self.assertEqual(one,two);self.assertEqual(one,[['a'],['b'],['c']])
        ab=compare_claims(a,b).model_copy(update={'identity':'same_event','origin':'manual'})
        bc=compare_claims(b,c).model_copy(update={'identity':'same_event','origin':'manual'})
        self.error('incomplete_cluster_confirmation',cluster_claims,data,CatalogConfig(),
            {('a','b'):ab.model_dump(mode='json'),('b','c'):bc.model_dump(mode='json')})

    def test_plan_is_read_only_repeat_cache_and_b1_grounding(self):
        a,b=self.publish('a'),self.publish('b',value='15')
        self.service.create_catalog()
        plan=self.service.plan_catalog_update([a,b]);self.assertEqual(plan['base_revision'],0)
        with self.store._transaction() as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM catalog_runs').fetchone()[0],0)
        first=self.service.apply_artifacts([a,b]);event=self.service.list_events()[0]
        self.assertEqual(len(event.claims),2);self.assertEqual(len(event.conflicts),1)
        validate_claim_collection(event.claims,event.sources,event.evidence,[self.store.get_article(s.source_id) for s in event.sources])
        again=self.service.apply_artifacts([b,a,a]);self.assertEqual(again['status'],'cached');self.assertEqual(again['revision'],1)
        self.assertEqual(self.service.list_events()[0],event)
        reopened=CatalogService(EventStore(self.db));self.assertEqual(reopened.list_events()[0],event)
        self.assertEqual(len(reopened.changes_since(0)['changes']),1)
        self.assertEqual(reopened.changes_since(1)['changes'],[])

    def test_new_revision_stale_drop_then_preserve_identity(self):
        a,b=self.publish('a'),self.publish('b',value='15');self.initialize(a,b)
        previous=self.service.list_events()[0]
        changed=self.store.get_current_article('a').article.model_dump(mode='json');changed['title']+=' уточнение'
        self.store.import_batch(json.dumps([changed]).encode())
        self.error('catalog_stale',self.service.list_events)
        self.assertEqual(self.service.get_event(previous.event_id,event_version=1),previous)
        self.service.rebuild_catalog();event=self.service.list_events()[0]
        self.assertEqual(event.event_id,previous.event_id);self.assertEqual(len(event.claims),1)
        self.assertFalse(event.conflicts);self.assertTrue(any('a' in l for l in event.limitations))
        old=self.service.list_events(revision=1)[0];self.assertEqual(len(old.claims),2)
        # A new B4 result of the correction restores a current witness, not an old quote.
        raw=case('a',changed['text'],[measurement('14','%')],entity='Банк России',expected={})
        raw['article']['article']=changed
        new=self.publish('a',raw=raw)
        self.service.apply_artifacts([new]);current=self.service.list_events()[0]
        self.assertEqual(current.event_id,previous.event_id);self.assertEqual(len(current.claims),2)
        self.assertEqual(current.event_version,3)

    def test_withdrawn_empty_result_and_restore(self):
        a=self.publish('a');self.initialize(a);event=self.service.list_events()[0]
        empty=self.publish('a',value='15',empty=True)
        self.service.apply_artifacts([empty]);self.assertEqual(self.service.list_events(),[])
        self.assertEqual(self.service.get_event(event.event_id).availability,'withdrawn')
        self.store.restore_version('a',1,'Restore original publication')
        self.service.rebuild_catalog();self.assertEqual(self.service.list_events(),[])
        a=self.publish('a');self.service.apply_artifacts([a])
        self.assertEqual(self.service.list_events()[0].event_id,event.event_id)

    def test_different_event_does_not_inherit_id(self):
        a=self.publish('a');self.initialize(a);old=self.service.list_events()[0]
        new=self.publish('a',day='2026-09-18');self.service.apply_artifacts([new])
        self.assertNotEqual(self.service.list_events()[0].event_id,old.event_id)
        self.assertEqual(self.service.get_event(old.event_id).availability,'withdrawn')

    def test_same_revision_replace_is_explicit(self):
        a=self.publish('a');self.initialize(a)
        new=self.publish('a',config=NormalizationConfig(dictionary=__import__('llm.events.normalization.core',fromlist=['resolved_dictionary']).resolved_dictionary(NormalizationConfig())|{'version':'test-b5'}))
        self.assertNotEqual(a,new)
        self.error('catalog_replace_required',self.service.apply_artifacts,[new])
        self.service.apply_artifacts([new],replace=True)
        self.error('ambiguous_catalog_input',self.service.apply_artifacts,[a,new],replace=True)

    def test_review_split_revoke_merge_and_history(self):
        a,b=self.publish('a'),self.publish('b');self.initialize(a,b)
        original=self.service.list_events()[0];ids=sorted(c.claim_id for c in original.claims)
        split=self.service.review_pair(ReviewRequest(left_id=ids[0],right_id=ids[1],identity='different_event',reason='Separate releases confirmed'))
        events=self.service.list_events();self.assertEqual(len(events),2)
        self.assertIn(original.event_id,[e.event_id for e in events])
        self.service.revoke_review(split['review_id'],'Reconsidered evidence')
        merged=self.service.list_events();self.assertEqual(len(merged),1);self.assertEqual(merged[0].event_id,original.event_id)
        self.assertEqual(len(self.service.list_events(include_withdrawn=True)),2)
        self.assertEqual(len(self.service.list_reviews()),2)
        self.error('review_not_active',self.service.revoke_review,split['review_id'],'Again')
        with self.store._transaction(write=True) as conn:
            with self.assertRaises(sqlite3.IntegrityError):conn.execute('DELETE FROM catalog_reviews')

    def test_manual_cannot_override_known_difference(self):
        a,b=self.publish('a'),self.publish('b',day='2026-09-18');self.initialize(a,b)
        ids=sorted(c.claim_id for e in self.service.list_events() for c in e.claims)
        self.error('incompatible_identity',self.service.review_pair,ReviewRequest(left_id=ids[0],right_id=ids[1],identity='same_event',reason='try'))
        self.assertEqual(self.service.list_reviews(),[])

    def test_limits_and_config_rebuild(self):
        a,b=self.publish('a'),self.publish('b');self.initialize(a)
        self.service.rebuild_catalog(CatalogConfig(max_claims=1))
        self.error('catalog_size_limit',self.service.apply_artifacts,[b])
        self.assertEqual(len(self.service.list_events()[0].claims),1)
        with patch('llm.events.catalog.service.implementation_hash',return_value='new-rules'):
            self.error('catalog_configuration_mismatch',self.service.apply_artifacts,[a])
            self.service.rebuild_catalog()

    def test_concurrent_publication_and_import_fencing(self):
        a,b=self.publish('a'),self.publish('b');self.initialize(a)
        first=self.service._prepare([b]);second=self.service._prepare([b])
        self.service._publish(first)
        self.error('catalog_changed',self.service._publish,second)
        prepared=self.service._prepare()
        raw=self.store.get_current_article('b').article.model_dump(mode='json');raw['title']+=' change'
        EventStore(self.db).import_batch(json.dumps([raw]).encode())
        self.error('catalog_changed',self.service._publish,prepared)

    def test_failed_publication_rolls_back(self):
        a=self.publish('a');self.service.create_catalog();prepared=self.service._prepare([a])
        # A failing trigger models interruption inside the publication transaction.
        with self.store._transaction(write=True) as conn:
            conn.execute("CREATE TRIGGER reject_members BEFORE INSERT ON catalog_memberships BEGIN SELECT RAISE(ABORT,'interrupt'); END")
        self.error('storage_integrity_error',self.service._publish,prepared)
        with self.store._transaction() as conn:
            for table in ('catalog_runs','catalog_events','catalog_event_versions','catalog_inputs'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
        with self.store._transaction(write=True) as conn:conn.execute('DROP TRIGGER reject_members')
        self.service.apply_artifacts([a]);self.assertEqual(len(self.service.list_events()),1)

    def test_input_lineage_rejects_intermediate(self):
        a=self.publish('a');artifact=self.store.get_artifact(a)
        owner=NormalizationService(self.store)._owner(artifact.attempt_id)
        upstream=self.store.get_artifact(owner.spec.configuration['input_artifact_id'])
        self.service.create_catalog()
        self.error('invalid_catalog_input',self.service.apply_artifacts,[upstream.artifact_id])
        source=self.store.get_current_article('a').article.model_dump(mode='json');source['title']+=' changed'
        self.store.import_batch(json.dumps([source]).encode())
        self.error('article_not_current',self.service.apply_artifacts,[a])

    def test_explicit_pair_advice_cache_and_no_catalog_mutation(self):
        a,b=self.publish('a'),self.publish('b');self.initialize(a,b)
        ids=sorted(c.claim_id for c in self.service.list_events()[0].claims)
        pair=sorted([c for v in self.service._prepare()['manifest']['contexts'] for c in [ClaimContext.model_validate(v)]],key=lambda c:c.claim.claim_id)
        response={'identity':'same_event','content':'duplicate_claim','reason':'Same dated decision',
                  'evidence_ids':[pair[0].evidence[0].evidence_id],'fields':[]}
        provider=OfflineProvider([self.response(response)])
        before=self.service._capture()['revision']
        result=self.service.check_pair(*ids,self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(result['status'],'succeeded');self.assertTrue(result['applicable'])
        self.assertEqual(self.service._capture()['revision'],before)
        viewed=self.service.get_recommendation(result['artifact_id']);self.assertTrue(viewed['applicable'])
        again=self.service.check_pair(*ids,self.profile,self.limits,provider=OfflineProvider([]),heartbeat=NoHeartbeat)
        self.assertEqual(again['status'],'cached');self.assertEqual(again['artifact_id'],result['artifact_id'])
        raw=self.store.get_current_article('b').article.model_dump(mode='json');raw['title']+=' changed'
        self.store.import_batch(json.dumps([raw]).encode())
        self.assertFalse(self.service.get_recommendation(result['artifact_id'])['applicable'])

    def test_pair_invalid_refs_json_and_length(self):
        pair=[ctx('a'),ctx('b')]
        self.error('invalid_pair_json',parse_recommendation,'{',pair)
        data={'identity':'same_event','content':'duplicate_claim','reason':'ok','evidence_ids':['missing'],'fields':[]}
        self.error('invalid_pair_reference',parse_recommendation,json.dumps(data),pair)
        data['evidence_ids']=['a:e'];data['fields']=[{'left_path':'/missing','right_path':'/text'}]
        self.error('invalid_pair_reference',parse_recommendation,json.dumps(data),pair)
        a,b=self.publish('a'),self.publish('b');self.initialize(a,b)
        ids=sorted(c.claim_id for c in self.service.list_events()[0].claims)
        outcome=self.service.check_pair(*ids,self.profile,self.limits,provider=OfflineProvider([self.response({},finish='length')]),heartbeat=NoHeartbeat)
        self.assertEqual(outcome['reason'],'output_limit_exceeded')
        self.assertFalse(any(j for j in self.service.list_reviews()))

    def test_pair_late_other_source_and_spend(self):
        a,b=self.publish('a'),self.publish('b');self.initialize(a,b)
        entries=[ClaimContext.model_validate(v) for v in self.service._prepare()['manifest']['contexts']]
        pair=sorted(entries,key=lambda c:c.claim.claim_id);ids=[c.claim.claim_id for c in pair]
        service=self.service;store=self.store
        class ChangingProvider(OfflineProvider):
            def complete(self,request):
                raw=store.get_current_article(pair[1].source.article_id).article.model_dump(mode='json');raw['title']+=' late edit'
                store.import_batch(json.dumps([raw]).encode())
                return super().complete(request)
        data={'identity':'same_event','content':'duplicate_claim','reason':'same','evidence_ids':[pair[0].evidence[0].evidence_id],'fields':[]}
        provider=ChangingProvider([self.response(data)])
        result=service.check_pair(*ids,self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(result['status'],'succeeded');self.assertFalse(result['applicable'])
        from llm.events.extraction.budget import CallLedger
        self.assertEqual(CallLedger(store,offline=True).get(result['call_id'])['state'],'settled')
        self.error('catalog_stale',service.list_events)

    def test_migration_v2_backup_retains_artifacts(self):
        legacy=Path(self.temp.name)/'legacy.sqlite3'
        with ExitStack() as stack:
            for name,value in [('DDL',V2_DDL),('TABLES',V2_TABLES),('SCHEMA_VERSION',2)]:stack.enter_context(patch('llm.events.storage.store.'+name,value))
            old=EventStore(legacy);raw=context('a')
            article={'article_id':'legacy','source':'Synthetic','url':None,'title':'Legacy','published_at':None,'language':'ru',
                     'text':'Legacy text','content_kind':'synthetic','is_full_original_text':False}
            sid=old.import_batch(json.dumps([article]).encode()).items[0].source_id
            job=old.enqueue(sid,build_processing_spec(self.profile));running=old.claim_next(job.spec,'test')
            from llm.events.extraction.budget import CallLedger
            ledger=CallLedger(old,offline=True)
            call_id=ledger.reserve(running.active_attempt.token,{'legacy':'request'},self.profile,self.limits)
            ledger.finish(call_id,response=self.response({}).payload())
            done=old.complete_attempt(running.active_attempt.token,{'legacy':'artifact'})
        self.error('storage_migration_required',EventStore,legacy)
        result=migrate_v2_to_v3(legacy)
        self.error('storage_migration_required',EventStore,legacy)
        migrate_v3_to_v4(legacy);reopened=EventStore(legacy)
        self.assertEqual(reopened.get_artifact(done.result_artifact_id).payload,{'legacy':'artifact'})
        self.assertEqual(reopened.get_article(sid).article.article_id,'legacy')
        self.assertEqual(CallLedger(reopened,offline=True).get(call_id)['state'],'settled')
        with closing(sqlite3.connect(result['backup_path'])) as conn:self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],2)
        self.error('unsupported_storage_schema',migrate_v2_to_v3,legacy)

    def test_cli_offline_and_gold_evaluation(self):
        command=[sys.executable,'-m','llm.events.catalog.cli','--db',str(Path(self.temp.name)/'no-db.sqlite3')]
        for sub in ('schema','check'):
            result=subprocess.run(command+[sub]+(['--check'] if sub=='schema' else []),capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse((Path(self.temp.name)/'no-db.sqlite3').exists())
        result=evaluate_gold(ROOT/'llm/events/fixtures/synthetic.json')
        self.assertEqual(result['articles'],20);self.assertIn('candidate_recall',result)
        self.assertIn('not B3',result['note'])

    def test_manual_archival_update_and_revocation(self):
        a=self.publish('a');self.initialize(a)
        previous=self.service.list_events()[0];old_id=previous.claims[0].claim_id
        new=self.publish('a',value='15');self.service.apply_artifacts([new])
        current=self.service.list_events()[0];new_id=current.claims[0].claim_id
        self.assertFalse(current.relations)
        left,right=sorted([old_id,new_id])
        reviewed=self.service.review_pair(ReviewRequest(left_id=left,right_id=right,identity='same_event',content='updates',
            from_id=new_id,to_id=old_id,reason='Explicitly confirmed correction of the publication'))
        record=self.service.list_events()[0]
        self.assertEqual([(r.from_id,r.to_id) for r in record.relations if r.type=='updates'],[(new_id,old_id)])
        self.assertEqual([c.claim_id for c in record.claims],[new_id])
        self.assertEqual(self.service.get_event(previous.event_id,event_version=1),previous)
        self.service.revoke_review(reviewed['review_id'],'Correction was not confirmed')
        self.assertFalse(self.service.list_events()[0].relations)

    def test_explicit_correction_auto_archival_update(self):
        a=self.publish('a');self.initialize(a)
        old=self.service.list_events()[0].claims[0]
        text='Исправление: Банк России утвердил решение 2026-10-23: ключевая ставка составляет 15% в России.'
        raw=case('a',text,[measurement('15','%')],entity='Банк России',expected={})
        new=self.publish('a',value='15',raw=raw);self.service.apply_artifacts([new])
        current=self.service.list_events()[0]
        self.assertEqual(len(current.relations),1)
        self.assertEqual(current.relations[0].type,'updates');self.assertEqual(current.relations[0].to_id,old.claim_id)
        self.assertFalse(current.conflicts)

    def test_synthetic_business_examples_preserve_conditions(self):
        # Severstal: improving a negative forecast never changes its sign.
        a,b=ctx('a',forecast=True,value='-0.5'),ctx('b',forecast=True,value='-0.5')
        for c in (a,b):
            c.claim.measurements[0].kind='relative_change';c.claim.measurements[0].value_role='after'
            c.claim.measurements.append(c.claim.measurements[0].model_copy(update={'measurement_id':c.claim.claim_id+':old','value':'-7','value_role':'before'}))
        self.assertEqual(compare_claims(a,b).content,'duplicate_claim')
        self.assertEqual(a.claim.measurements[0].value,'-0.5')
        # Gazprom: both sides of a level revision remain positive and differ in role.
        a,b=ctx('a',value='1100000000000'),ctx('b',value='1096000000000')
        a.claim.measurements[0].value_role='before';b.claim.measurements[0].value_role='after'
        self.assertEqual(compare_claims(a,b).content,'complements')
        # Oil: one current indicator, different comparison bases.
        a,b=ctx('a','market_indicator',value='20'),ctx('b','market_indicator',value='-8.5')
        from llm.events.contracts.models import Period
        for x,start,end in ((a,'2025-09-01','2025-09-30'),(b,'2026-08-01','2026-08-31')):
            x.claim.measurements[0].kind='relative_change'
            x.claim.measurements[0].comparison_period=Period(start=start,end=end,text=start)
        self.assertEqual(compare_claims(a,b).content,'complements')
        b.claim.participants[0].entity_id='org:brazil_supplier'
        self.assertEqual(compare_claims(a,b).identity,'different_event')
        # RSHB: observed income and a full-year forecast are distinct identities.
        a,b=ctx('a','company_results'),ctx('b','company_results',forecast=True)
        self.assertEqual(compare_claims(a,b).identity,'different_event')
        # Reporting standards and company/segment scope are not collapsed.
        a,b=ctx('a','company_results'),ctx('b','company_results')
        b.claim.measurements[0].metric='net_profit_ifrs'
        self.assertEqual(compare_claims(a,b).identity,'different_event')

    def test_related_stage_requires_explicit_document(self):
        a,b=ctx('a','regulation'),ctx('b','regulation')
        a.claim.stage='effective';b.claim.stage='approved'
        self.assertIsNone(related_stages(a,b))
        for c in (a,b):
            c.evidence[0].quote+=' Постановление №123.';c.evidence[0].end=len(c.evidence[0].quote)
        self.assertEqual(related_stages(a,b),('a','b'))
        self.assertEqual(compare_claims(a,b).identity,'different_event')

    def test_unknown_periods_and_exchange_rate_kind(self):
        a,b=ctx('a'),ctx('b')
        from llm.events.contracts.models import Period
        for c in (a,b):c.claim.measurements[0].comparison_period=Period(start=None,end=None,text='к прежнему периоду')
        self.assertEqual(compare_claims(a,b).content,'complements')
        a,b=ctx('a','fx_rate'),ctx('b','fx_rate')
        b.claim.measurements[0].metric='market_exchange_rate'
        self.assertEqual(compare_claims(a,b).identity,'different_event')
        for c in (a,b):c.claim.measurements[0].metric='exchange_rate'
        self.assertEqual(compare_claims(a,b).identity,'uncertain')

    def test_pair_failures_no_hidden_retries(self):
        a,b=self.publish('a'),self.publish('b');self.initialize(a,b)
        ids=sorted(c.claim_id for c in self.service.list_events()[0].claims)
        scenarios=[self.response({},status=429),self.response({},status=500),
                   self.response({'extra':True}),ProviderFailure('provider_timeout','Timeout')]
        for i,response in enumerate(scenarios):
            provider=OfflineProvider([response])
            profile=self.profile.model_copy(update={'model':f'offline:failure:{i}'})
            result=self.service.check_pair(*ids,profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
            self.assertIn(result['status'],('failed','deferred'))
            self.assertEqual(len(provider.requests),1)
        self.assertEqual(self.service._capture()['revision'],1)

    def test_pair_lost_anchor_lease_keeps_spend_without_success(self):
        a,b=self.publish('a'),self.publish('b');self.initialize(a,b)
        ids=sorted(c.claim_id for c in self.service.list_events()[0].claims)
        store=self.store;clock=self.clock
        class ExpiringProvider(OfflineProvider):
            def complete(self,request):
                clock.value+=901;store.requeue_expired()
                return super().complete(request)
        result=self.service.check_pair(*ids,self.profile,self.limits,
            provider=ExpiringProvider([self.response({})]),heartbeat=NoHeartbeat)
        self.assertEqual(result['status'],'superseded')
        from llm.events.extraction.budget import CallLedger
        self.assertEqual(CallLedger(store,offline=True).get(result['call_id'])['state'],'settled')
        self.assertNotIn('artifact_id',result)

    def test_migration_failure_rolls_back_v2(self):
        legacy=Path(self.temp.name)/'broken.sqlite3'
        with ExitStack() as stack:
            for name,value in [('DDL',V2_DDL),('TABLES',V2_TABLES),('SCHEMA_VERSION',2)]:stack.enter_context(patch('llm.events.storage.store.'+name,value))
            EventStore(legacy)
        with patch('llm.events.storage.migration.B5_DDL',['CREATE TABLE temp_b5(x)','INVALID SQL']):
            self.error('storage_error',migrate_v2_to_v3,legacy)
        with closing(sqlite3.connect(legacy)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='temp_b5'").fetchone())
        migrate_v2_to_v3(legacy);migrate_v3_to_v4(legacy);EventStore(legacy)

    def test_two_connections_only_one_snapshot_publishes(self):
        a,b=self.publish('a'),self.publish('b');self.initialize(a)
        other=CatalogService(EventStore(self.db,clock=self.clock))
        plans=[self.service._prepare([b]),other._prepare([b])]
        def publish(service,plan):
            try:return service._publish(plan)['status']
            except StorageError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(lambda item:publish(*item),[(self.service,plans[0]),(other,plans[1])]))
        self.assertEqual(sorted(outcomes),['catalog_changed','succeeded'])
        self.assertEqual(self.service._capture()['revision'],2)

    def test_pair_guard_race_defers_without_call(self):
        a,b=self.publish('a'),self.publish('b');self.initialize(a,b)
        pairs=sorted([ClaimContext.model_validate(v) for v in self.service._prepare()['manifest']['contexts']],key=lambda c:c.claim.claim_id)
        ids=[c.claim.claim_id for c in pairs]
        from llm.events.extraction.budget import CallLedger
        original=CallLedger.reserve
        store=self.store
        def reserve(ledger,*args,**kwargs):
            raw=store.get_current_article(pairs[1].source.article_id).article.model_dump(mode='json');raw['title']+=' guard race'
            store.import_batch(json.dumps([raw]).encode())
            return original(ledger,*args,**kwargs)
        provider=OfflineProvider([])
        with patch.object(CallLedger,'reserve',reserve):
            result=self.service.check_pair(*ids,self.profile,self.limits,provider=provider,heartbeat=NoHeartbeat)
        self.assertEqual(result['status'],'deferred');self.assertEqual(result['reason'],'pair_stale')
        self.assertEqual(provider.requests,[])
        self.assertEqual(self.store.get_job(result['job_id']).state,'deferred')

    def test_pair_heartbeat_renews_anchor(self):
        a,b=self.publish('a'),self.publish('b');self.initialize(a,b)
        pairs=sorted([ClaimContext.model_validate(v) for v in self.service._prepare()['manifest']['contexts']],key=lambda c:c.claim.claim_id)
        ids=[c.claim.claim_id for c in pairs]
        from llm.events.extraction.runner import LeaseHeartbeat
        active=[];clock=self.clock;store=self.store
        class ControlledHeartbeat(LeaseHeartbeat):
            def __enter__(self):active.append(self);return self
            def __exit__(self,*_):pass
        class SlowProvider(OfflineProvider):
            def complete(self,request):
                clock.value+=600;active[0].pulse();clock.value+=400
                self.expired=store.requeue_expired()
                return super().complete(request)
        reply={'identity':'same_event','content':'duplicate_claim','reason':'same',
               'evidence_ids':[pairs[0].evidence[0].evidence_id],'fields':[]}
        provider=SlowProvider([self.response(reply)])
        result=self.service.check_pair(*ids,self.profile,self.limits,provider=provider,heartbeat=ControlledHeartbeat)
        self.assertEqual(result['status'],'succeeded');self.assertEqual(provider.expired,[])

    def test_missing_invalidation_trigger_is_rejected(self):
        with self.store._transaction(write=True) as conn:conn.execute('DROP TRIGGER catalog_article_invalidated')
        self.error('unsupported_storage_schema',EventStore,self.db)

    def test_manual_historical_identity_preserves_original_id(self):
        a=self.publish('a');self.initialize(a)
        original=self.service.list_events()[0];old_id=original.claims[0].claim_id
        text='Банк России сообщает: ключевая ставка составляет 15% в России.'
        raw=case('a',text,[measurement('15','%')],entity='Банк России',expected={})
        # Deliberately leave the new decision date unknown while retaining a
        # valid B4 structure. It must not inherit identity until explicit review.
        c=raw['input']['extraction']['claims'][0]
        c.update(stage='approved',action='keep',object='policy_rate',geographies=['RU'])
        for field in ('stage','action','object','geographies'):c['field_evidence']['/'+field]=['e1']
        sid=self.store.import_batch(json.dumps([raw['article']['article']|{'article_id':'a'}],ensure_ascii=False).encode()).items[0].source_id
        spec=build_processing_spec(self.profile);job=self.store.enqueue(sid,spec)
        outcome=ExtractionRunner(self.store,OfflineProvider([self.response(raw['input']['extraction'])]),heartbeat=NoHeartbeat).run_next(spec,self.limits,source_id=sid)
        self.assertEqual(outcome.status,'succeeded')
        upstream=self.store.get_job(job.job_id).result_artifact_id
        artifact=NormalizationService(self.store).normalize_artifact(upstream)['artifact_id']
        self.service.apply_artifacts([artifact]);new=self.service.list_events()[0]
        self.assertNotEqual(original.event_id,new.event_id)
        new_id=new.claims[0].claim_id;left,right=sorted([old_id,new_id])
        self.service.review_pair(ReviewRequest(left_id=left,right_id=right,identity='same_event',content='updates',
            from_id=new_id,to_id=old_id,reason='Decision identity independently reviewed'))
        self.assertEqual(self.service.list_events()[0].event_id,original.event_id)
