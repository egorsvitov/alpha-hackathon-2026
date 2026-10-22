"""Durable v2 plans, one-call jobs, local A/B assembly and isolated publication."""
from copy import deepcopy
from pathlib import Path
from uuid import NAMESPACE_URL,uuid5
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from uuid import uuid4
import llm.events.extraction.staged as core
import llm.events.extraction.staged_models as m

from llm.events.extraction.protocol import digest, parse_extraction
from llm.events.extraction.models import ProviderProfile, ExtractionResult
from llm.events.contracts.models import Issue
from llm.events.storage.store import StorageError, canonical_json, processing_hash
from llm.events.storage.models import ProcessingSpec
from llm.events.extraction.runner import OneShotRunner
from llm.events.normalization.service import NormalizationService

from llm.events.catalog.service import CatalogService
from llm.events.publication.service import EventBundleService
from llm.events.extraction.credits import RequestCounter, extract_one
from llm.events.contracts.validation import article_sha256

PROCESSORS={'discover':'events.discover_mentions.v2','templates':'events.extract_template_batch.v2','verify':'events.verify_claims.v2'}


def implementation():
    return digest({name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in
        ('staged.py','staged_models.py','prompts.py','service.py',
         'fragments.py','transport.py','references.py')})


def owner_for(store,artifact):
    with store._transaction() as c:
        row=c.execute('SELECT job_id FROM attempts WHERE attempt_id=?',(artifact.attempt_id,)).fetchone()
    if row is None:raise StorageError('input_provenance_mismatch','Artifact has no job')
    return store.get_job(row['job_id'])


def read_final(store,artifact_id,record,*,processor=None,kind=None):
    artifact=store.get_artifact(artifact_id);owner=owner_for(store,artifact)
    if (owner.state!='succeeded' or owner.result_artifact_id!=artifact_id or owner.source_id!=record.source_id
        or (processor and owner.spec.processor_name!=processor) or (kind and artifact.kind!=kind)):
        raise StorageError('invalid_v2_input','Expected successful final artifact from this source')
    payload=artifact.payload
    if isinstance(payload,dict) and payload.get('processing_spec_hash',owner.spec_hash)!=owner.spec_hash:
        raise StorageError('input_provenance_mismatch','Payload processing spec differs from owner')
    if isinstance(payload,dict) and (payload.get('source_id',record.source_id)!=record.source_id or
       payload.get('article_sha256',article_sha256(record.article))!=article_sha256(record.article)):
        raise StorageError('input_provenance_mismatch','Source/hash differs from archive')
    return artifact,owner


def current(store,record):
    if store.get_current_article(record.article.article_id).source_id!=record.source_id:
        raise StorageError('article_not_current','Historical sources require an explicit audit path')


def local_result(store,record,spec,payload,kind,schema_version,sidecars=None):
    current(store,record)
    job=store.enqueue(record.source_id,spec)
    if job.state=='succeeded':return {'status':'cached','job_id':job.job_id,'artifact_id':job.result_artifact_id}
    if job.state!='pending':return {'status':job.state,'job_id':job.job_id,'reason':'explicit_retry_required'}
    active=store.claim_next(spec,'v2-local-assembler',source_id=record.source_id)
    if active is None:return {'status':'running','job_id':job.job_id}
    token=active.active_attempt.token
    for typ,sidecar in sidecars or []:store.save_artifact(token,typ,sidecar,schema_version='two-stage.v2')
    completed=store.complete_attempt(token,payload,kind=kind,schema_version=schema_version)
    return {'status':'succeeded','job_id':completed.job_id,'artifact_id':completed.result_artifact_id,'api_calls':0}


class StageRunner(OneShotRunner):
    def __init__(self,store,provider,*,stage,plan_artifact_id=None,batch_id=None,
                 normalized_artifact_id=None,candidate_ids=None,**kwargs):
        super().__init__(store,provider,**kwargs)
        if stage not in PROCESSORS:raise StorageError('invalid_stage','Unknown v2 stage')
        self.stage=stage;self.plan_artifact_id=plan_artifact_id;self.batch_id=batch_id
        self.normalized_artifact_id=normalized_artifact_id;self.candidate_ids=candidate_ids

    def _plan(self,record=None):
        artifact=self.store.get_artifact(self.plan_artifact_id);owner=owner_for(self.store,artifact)
        record=record or self.store.get_article(owner.source_id)
        read_final(self.store,artifact.artifact_id,record,processor='events.plan_extraction.v2',kind='extraction_plan')
        plan=core.validate(m.ExtractionPlan,artifact.payload);core.validate_origin(plan,record)
        active_profile=getattr(self,'expected_profile',getattr(self.provider,'profile',None))
        expected=digest({'implementation':implementation(),'profile':active_profile.model_dump(mode='json')}) if active_profile is not None else None
        # Exact request hashes below also protect offline providers without a bound profile.
        if expected is not None and expected!=plan.configuration_sha256:
            raise StorageError('plan_changed','Plan belongs to another implementation/profile')
        discovery_artifact,_=read_final(self.store,plan.discovery_artifact_id,record,processor=PROCESSORS['discover'],kind='discovered_mentions')
        discovery=core.validate(m.DiscoveryResult,discovery_artifact.payload)
        if plan.discovery_sha256 not in (digest(discovery_artifact.payload),digest(discovery.model_dump(mode='json'))):raise StorageError('input_provenance_mismatch','Discovery changed')
        matches=[b for b in plan.batches if b.batch_id==self.batch_id]
        if len(matches)!=1:raise StorageError('batch_not_found','Unknown planned batch')
        return record,plan,discovery,matches[0]

    def _normalized(self,record=None):
        artifact=self.store.get_artifact(self.normalized_artifact_id);owner=owner_for(self.store,artifact)
        record=record or self.store.get_article(owner.source_id)
        # Reuse B5's complete provenance validation, without creating a catalog.
        data=CatalogService(self.store)._load_artifact(artifact.artifact_id).model_dump(mode='json')
        if data['source']['source_id']!=record.source_id:raise StorageError('input_provenance_mismatch','Verification source differs')
        ids=list(self.candidate_ids or [])
        selected=[a for a in data['accepted'] if a['candidate_id'] in ids]
        if not ids or len(set(ids))!=len(ids) or len(selected)!=len(ids) or len(ids)>2:
            raise StorageError('invalid_verification_batch','Expected one or two unique accepted candidates')
        return record,data,selected

    def build_spec(self,profile):
        profile=ProviderProfile.model_validate(profile)
        self.expected_profile=profile
        if profile.max_tokens>4096:raise StorageError('invalid_v2_profile','Initial output cap is 4096')
        config={'version':core.VERSION,'stage':self.stage,'profile':profile.model_dump(mode='json'),
            'implementation_sha256':implementation(),'reference_format':'span-ids.v1'}
        if self.stage=='templates':
            _,plan,discovery,batch=self._plan()
            config.update(plan_artifact_id=self.plan_artifact_id,plan_sha256=digest(plan.model_dump(mode='json')),
                          batch_id=self.batch_id,request_sha256=batch.request_sha256)
        elif self.stage=='verify':
            _,data,selected=self._normalized()
            config.update(normalized_artifact_id=self.normalized_artifact_id,normalized_sha256=digest(data),candidate_ids=self.candidate_ids)
        return ProcessingSpec(processor_name=PROCESSORS[self.stage],processor_version=core.VERSION,configuration=config)

    def profile_for_spec(self,spec):
        profile=ProviderProfile.model_validate(spec.configuration['profile'])
        if spec!=self.build_spec(profile):raise StorageError('processing_spec_mismatch','Stage implementation/input changed')
        return profile

    def request_for(self,record,profile):
        if self.stage=='templates':
            _,_,discovery,batch=self._plan(record)
            if batch.deferred_reason:raise StorageError(batch.deferred_reason,'Batch was deferred during planning')
            request=core.request_for('templates',record,profile,mentions=discovery.mentions,batch=batch)
            if digest(request)!=batch.request_sha256:raise StorageError('plan_changed','Request differs from saved plan')
            return request
        if self.stage=='verify':
            _,data,selected=self._normalized(record)
            return core.request_for('verify',record,profile,review=core.verification_input(selected,data))
        return core.request_for('discover',record,profile)

    def output_for(self,content,record,spec):
        if self.stage=='discover':
            result=core.parse_discovery(content,record,spec)
            return result.model_dump(mode='json'),'discovered_mentions','discovery.v2'
        if self.stage=='templates':
            _,_,discovery,batch=self._plan(record)
            result=core.compile_batch(content,record,discovery,batch)
            result.update(source_id=record.source_id,article_sha256=article_sha256(record.article),
                          plan_artifact_id=self.plan_artifact_id,batch_id=self.batch_id,processing_spec_hash=processing_hash(spec))
            result=m.TemplateBatchResult.model_validate(result).model_dump(mode='json')
            return result,'template_batch','template-batch.v2'
        _,data,selected=self._normalized(record)
        result=core.parse_verification(content,record,self.normalized_artifact_id,selected)
        return result.model_dump(mode='json'),'semantic_review','semantic-review.v1'


class ExperimentCounter(RequestCounter):
    MAX_REQUESTS=60

    def freeze_profile(self,profile):
        with closing(sqlite3.connect(self.path)) as conn,conn:
            conn.execute('CREATE TABLE IF NOT EXISTS experiment_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            value=canonical_json(profile.model_dump(mode='json'))
            conn.execute("INSERT OR IGNORE INTO experiment_settings VALUES ('profile',?)",(value,))
            if conn.execute("SELECT value FROM experiment_settings WHERE key='profile'").fetchone()[0]!=value:
                raise StorageError('experiment_profile_changed','Profile is frozen for this journal')

    def reserve(self,request):
        probe=(request.get('messages') or [{}])[0].get('content','').startswith('FORMAT_PROBE_V2')
        with closing(sqlite3.connect(self.path,isolation_level=None)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            limit=conn.execute('SELECT max_requests FROM settings WHERE id=1').fetchone()[0]
            if conn.execute('SELECT COUNT(*) FROM calls').fetchone()[0]>=limit:
                raise StorageError('pilot_limit_exhausted','All request slots are consumed')
            if probe:
                frozen=conn.execute("SELECT name FROM sqlite_master WHERE name='experiment_settings'").fetchone()
                if frozen and conn.execute("SELECT 1 FROM experiment_settings WHERE key='profile'").fetchone():
                    raise StorageError('experiment_profile_frozen','Probe before starting extraction')
                count=sum((json.loads(r[0]).get('messages') or [{}])[0].get('content','').startswith('FORMAT_PROBE_V2')
                          for r in conn.execute('SELECT request_json FROM calls'))
                if count>=2:raise StorageError('probe_limit_exhausted','Only two capability probes per experiment')
            call_id=str(uuid4())
            conn.execute('INSERT INTO calls(call_id,created_at,request_json) VALUES (?,?,?)',(call_id,time.time(),canonical_json(request)))
            conn.commit();return call_id


class ExtractionService:
    def __init__(self,store,provider,profile,*,limits=None,counter=None,heartbeat=None):
        self.store=store;self.provider=provider;self.profile=ProviderProfile.model_validate(profile)
        self.limits=limits;self.counter=counter;self.heartbeat=heartbeat
        if self.profile!=getattr(provider,'profile',self.profile):raise StorageError('provider_profile_mismatch','Provider/profile differ')
        if counter is None and limits is None:raise StorageError('execution_limits_required','Supply standard limits or explicit pilot counter')

    def _worker(self,**kwargs):
        if self.heartbeat is not None:kwargs['heartbeat']=self.heartbeat
        return StageRunner(self.store,self.provider,**kwargs)

    def _call(self,record,worker):
        current(self.store,record)
        if self.counter is not None:
            if not self.provider.is_live:raise StorageError('execution_mode_mismatch','Credit pilot uses a live provider')
            # Separate explicit credit mode; standard monetary gates remain unchanged.
            if not isinstance(worker,FormatProbeRunner):self.counter.freeze_profile(self.profile)
            try:return extract_one(self.store,record,self.profile,self.counter,self.provider,processor=worker)
            except StorageError as exc:
                if exc.code=='pilot_limit_exhausted':return {'status':'deferred','reason':exc.code,'stop':True}
                raise
        spec=worker.build_spec(self.profile);job=self.store.enqueue(record.source_id,spec)
        if job.state=='succeeded':return {'status':'cached','job_id':job.job_id,'artifact_id':job.result_artifact_id}
        if job.state!='pending':return {'status':job.state,'job_id':job.job_id,'reason':'explicit_retry_required'}
        result=worker.run_next(spec,self.limits,source_id=record.source_id).model_dump(mode='json')
        result['artifact_id']=self.store.get_job(job.job_id).result_artifact_id
        return result

    def discover(self,source_id):
        return self._call(self.store.get_article(source_id),self._worker(stage='discover'))

    def plan_extraction(self,discovery_artifact_id,*,persist=True):
        artifact=self.store.get_artifact(discovery_artifact_id);record=self.store.get_article(owner_for(self.store,artifact).source_id)
        current(self.store,record)
        read_final(self.store,discovery_artifact_id,record,processor=PROCESSORS['discover'],kind='discovered_mentions')
        discovery=core.validate(m.DiscoveryResult,artifact.payload)
        worker=self._worker(stage='discover');spec=worker.build_spec(self.profile)
        config_hash=digest({'implementation':implementation(),'profile':self.profile.model_dump(mode='json')})
        plan=core.plan_extraction(record,discovery,discovery_artifact_id,self.profile,config_hash)
        if not persist:return plan.model_dump(mode='json')
        spec=ProcessingSpec(processor_name='events.plan_extraction.v2',processor_version=core.VERSION,
            configuration={'plan_sha256':digest(plan.model_dump(mode='json'))})
        result=local_result(self.store,record,spec,plan.model_dump(mode='json'),'extraction_plan','extraction-plan.v2')
        return {**result,'plan':plan.model_dump(mode='json')}

    def run_extraction(self,plan_artifact_id,*,max_jobs):
        if type(max_jobs) is not int or max_jobs<=0:raise StorageError('invalid_argument','max_jobs must be positive')
        artifact=self.store.get_artifact(plan_artifact_id);record=self.store.get_article(owner_for(self.store,artifact).source_id)
        current(self.store,record);read_final(self.store,plan_artifact_id,record,processor='events.plan_extraction.v2',kind='extraction_plan')
        plan=core.validate(m.ExtractionPlan,artifact.payload);out=[];executed=0
        for batch in plan.batches:
            if batch.deferred_reason:
                out.append({'batch_id':batch.batch_id,'status':'deferred','reason':batch.deferred_reason});continue
            worker=self._worker(stage='templates',plan_artifact_id=plan_artifact_id,batch_id=batch.batch_id)
            job=self.store.enqueue(record.source_id,worker.build_spec(self.profile))
            if job.state=='pending' and executed>=max_jobs:
                out.append({'batch_id':batch.batch_id,'status':'deferred','reason':'run_limit'});continue
            if job.state=='pending':executed+=1
            result=self._call(record,worker)
            out.append({**result,'batch_id':batch.batch_id})
            if result.get('stop'):break
        return out

    def _selected_batches(self,plan_artifact_id,artifact_ids):
        artifact=self.store.get_artifact(plan_artifact_id);record=self.store.get_article(owner_for(self.store,artifact).source_id)
        read_final(self.store,plan_artifact_id,record,processor='events.plan_extraction.v2',kind='extraction_plan')
        plan=core.validate(m.ExtractionPlan,artifact.payload);selected={}
        for aid in artifact_ids:
            a,owner=read_final(self.store,aid,record,processor=PROCESSORS['templates'],kind='template_batch')
            bid=a.payload.get('batch_id')
            if a.payload.get('plan_artifact_id')!=plan_artifact_id or bid not in {b.batch_id for b in plan.batches}:
                raise StorageError('input_provenance_mismatch','Batch belongs to another plan')
            if owner.spec.configuration.get('plan_artifact_id')!=plan_artifact_id or owner.spec.configuration.get('batch_id')!=bid:
                raise StorageError('input_provenance_mismatch','Batch payload differs from owning job')
            if bid in selected:raise StorageError('duplicate_batch','Select one artifact per planned batch')
            selected[bid]=a
        return record,plan,selected

    def assemble_variant(self,plan_artifact_id,batch_artifact_ids,*,variant='A',
                         normalized_artifact_id=None,verification_artifact_ids=None):
        if variant not in ('A','B'):raise StorageError('invalid_variant','Expected A or B')
        record,plan,selected=self._selected_batches(plan_artifact_id,batch_artifact_ids);current(self.store,record)
        claims=[];evidence={};coverage=[];issues=[];lineage=[];limitations=[core.PROVENANCE]
        discovery=m.DiscoveryResult.model_validate(self.store.get_artifact(plan.discovery_artifact_id).payload)
        coverage.extend(discovery.rejected_mentions)
        for batch in plan.batches:
            a=selected.get(batch.batch_id)
            if a is None:
                status='deferred' if batch.deferred_reason else 'missing'
                for mid in batch.mention_ids:coverage.append(m.Coverage(mention_id=mid,status=status,issues=[
                    Issue(code=batch.deferred_reason or 'batch_unfinished',path='/mentions/'+mid,message='No selected completed batch')]))
                continue
            data=a.payload
            claims.extend(deepcopy(data['extraction']['claims']));evidence.update({e['evidence_id']:e for e in data['extraction']['evidence']})
            coverage.extend(m.Coverage.model_validate(x) for x in data['coverage'])
            issues.extend(Issue.model_validate(i) for i in data['issues']);lineage.append(a.artifact_id)
            limitations.extend(data['extraction']['limitations'])
        excluded=[]
        if variant=='B':
            if normalized_artifact_id is None:raise StorageError('missing_variant_a','Variant B requires normalized A')
            normalized=CatalogService(self.store)._load_artifact(normalized_artifact_id).model_dump(mode='json')
            upstream,_,a_result,a_record=NormalizationService(self.store).input_artifact(normalized['input_artifact_id'])
            a_owner=owner_for(self.store,upstream)
            aconfig=a_owner.spec.configuration
            if (a_record!=record or aconfig.get('variant')!='A' or aconfig.get('plan_artifact_id')!=plan_artifact_id or
                sorted(aconfig.get('batch_artifact_ids',[]))!=sorted(batch_artifact_ids)):
                raise StorageError('input_provenance_mismatch','Verification input is not this exact A assembly')
            decisions={};lineage.append(normalized_artifact_id)
            for aid in verification_artifact_ids or []:
                a,owner=read_final(self.store,aid,record,processor=PROCESSORS['verify'],kind='semantic_review')
                review=core.validate(m.VerificationResult,a.payload)
                if review.normalized_artifact_id!=normalized_artifact_id or owner.spec.configuration.get('normalized_artifact_id')!=normalized_artifact_id:
                    raise StorageError('input_provenance_mismatch','Review belongs to another A result')
                for selection in review.selections:
                    if selection.candidate_id in decisions:raise StorageError('duplicate_verification','Multiple reviews for one candidate')
                    decisions[selection.candidate_id]=selection
                lineage.append(aid)
            accepted_a={a['candidate_id'] for a in normalized['accepted']}
            keep={cid for cid,s in decisions.items() if s.accepted and cid in accepted_a}
            excluded=[c['claim_id'] for c in claims if c['claim_id'] not in keep]
            for cid in sorted(accepted_a-keep):
                issues.extend(decisions[cid].issues if cid in decisions else [Issue(code='verification_missing',path='/candidates/'+cid,message='Candidate not verified')])
            # Reuse the original B3 candidates byte-for-byte; remove whole claims only.
            claims=[c for c in claims if c['claim_id'] in keep]
            for cov in coverage:
                rejected=set(cov.candidate_ids)-keep
                if rejected:
                    cov.issues.append(Issue(code='semantic_selection_excluded',path='/mentions/'+cov.mention_id,message='One or more candidates were excluded'))
                    cov.candidate_ids=[cid for cid in cov.candidate_ids if cid in keep]
                    if not cov.candidate_ids:cov.status='rejected'
            limitations.append('Variant B: model-based source-support selection, not independent factual verification.')
        used={eid for c in claims for eid in c['evidence_ids']}
        extraction=parse_extraction(canonical_json({'claims':claims,'evidence':[evidence[k] for k in sorted(used)],'excluded':[],
            'limitations':sorted(set(limitations))}))
        incomplete=bool(issues) or any(c.status in ('unresolved','rejected','deferred','missing') or c.issues for c in coverage)
        audit=m.ExtractionAudit(source_id=record.source_id,article_sha256=article_sha256(record.article),variant=variant,
            coverage=coverage,issues=issues,excluded_candidate_ids=excluded,input_artifact_ids=[plan_artifact_id]+sorted(set(lineage)),incomplete=incomplete)
        config={'version':core.VERSION,'implementation_sha256':implementation(),'variant':variant,
            'plan_artifact_id':plan_artifact_id,'batch_artifact_ids':sorted(batch_artifact_ids),
            'normalized_a_artifact_id':normalized_artifact_id,'verification_artifact_ids':sorted(verification_artifact_ids or []),
            'audit_sha256':digest(audit.model_dump(mode='json'))}
        spec=ProcessingSpec(processor_name='events.extract_candidates',processor_version='b3.6-local-'+variant,configuration=config)
        result=ExtractionResult(source_id=record.source_id,article_sha256=article_sha256(record.article),
            processing_spec_hash=processing_hash(spec),extraction=extraction)
        saved=local_result(self.store,record,spec,result.model_dump(mode='json'),'extraction_candidates','b3.1',
                           sidecars=[('extraction_audit',audit.model_dump(mode='json'))])
        with self.store._transaction() as c:
            ids=c.execute("SELECT ar.artifact_id FROM artifacts ar JOIN attempts a ON a.attempt_id=ar.attempt_id WHERE ar.attempt_id=(SELECT attempt_id FROM artifacts WHERE artifact_id=?) AND ar.kind='extraction_audit'",(saved['artifact_id'],)).fetchall()
        if len(ids)!=1:raise StorageError('ambiguous_audit','Expected exactly one immutable assembly audit')
        saved.update(audit_artifact_id=ids[0][0],incomplete=incomplete,candidate_count=len(claims),variant=variant)
        return saved

    def normalize_variant(self,assembly):
        return NormalizationService(self.store).normalize_artifact(assembly['artifact_id'],input_audit_artifact_id=assembly['audit_artifact_id'])

    def verify_extraction(self,normalized_artifact_id,*,max_jobs):
        if type(max_jobs) is not int or max_jobs<=0:raise StorageError('invalid_argument','max_jobs must be positive')
        artifact=self.store.get_artifact(normalized_artifact_id);record=self.store.get_article(owner_for(self.store,artifact).source_id)
        current(self.store,record);normalized=CatalogService(self.store)._load_artifact(normalized_artifact_id).model_dump(mode='json')
        upstream=owner_for(self.store,self.store.get_artifact(normalized['input_artifact_id']))
        if upstream.spec.configuration.get('variant')!='A':raise StorageError('invalid_variant_a','Only A can be verified')
        ids=sorted(a['candidate_id'] for a in normalized['accepted']);out=[];executed=0
        for i in range(0,len(ids),2):
            worker=self._worker(stage='verify',normalized_artifact_id=normalized_artifact_id,candidate_ids=ids[i:i+2])
            job=self.store.enqueue(record.source_id,worker.build_spec(self.profile))
            if job.state=='pending' and executed>=max_jobs:
                out.append({'status':'deferred','reason':'run_limit','candidate_ids':ids[i:i+2]});continue
            if job.state=='pending':executed+=1
            out.append(self._call(record,worker))
            if out[-1].get('stop'):break
        return out

    def publish_variant(self,variant,normalized_artifact_ids,*,experiment_id):
        if variant not in ('A','B'):raise StorageError('invalid_variant','Expected A or B')
        catalog_id='experiment-v2-'+str(uuid5(NAMESPACE_URL,experiment_id+':'+variant+':'+digest(sorted(normalized_artifact_ids))))
        for aid in normalized_artifact_ids:
            result=CatalogService(self.store)._load_artifact(aid)
            upstream=owner_for(self.store,self.store.get_artifact(result.input_artifact_id))
            if upstream.spec.configuration.get('variant')!=variant:raise StorageError('variant_mismatch','Mixed A/B input')
        catalog=CatalogService(self.store,catalog_id);catalog.create_catalog();applied=catalog.apply_artifacts(normalized_artifact_ids)
        bundles=EventBundleService(self.store,catalog_id);publication=bundles.publish_snapshot()
        return {'catalog_id':catalog_id,'catalog':applied,'publication':publication,'bundles':bundles.export_events()}


class FormatProbeRunner(StageRunner):
    def __init__(self,store,provider,**kwargs):super().__init__(store,provider,stage='discover',**kwargs)

    def build_spec(self,profile):
        base=super().build_spec(profile)
        return base.model_copy(update={'processor_name':'events.format_probe.v2'})

    def request_for(self,record,profile):
        request=core.request_for('discover',record,profile)
        schema={'type':'object','properties':{'ok':{'type':'boolean','const':True}},'required':['ok'],'additionalProperties':False}
        request['messages']=[{'role':'system','content':'FORMAT_PROBE_V2 Return only the JSON object {"ok":true}. No Markdown.'},
                             {'role':'user','content':'This is a format compatibility check, not news extraction.'}]
        if profile.response_format=='json_schema':
            request['response_format']['json_schema']['schema']=schema
        request['max_tokens']=128
        return request

    def output_for(self,content,record,spec):
        from llm.events.extraction.transport import decode_stage_json
        raw,envelope=decode_stage_json(content)
        if raw!={'ok':True} or type(raw.get('ok')) is not bool or envelope!='json':
            raise StorageError('format_probe_invalid','Expected bare JSON matching probe schema')
        return {'source_id':record.source_id,'article_sha256':article_sha256(record.article),
            'observed_compatible':True,'format':spec.configuration['profile']['response_format'],
            'decoding_guarantee':False},'format_probe','format-probe.v1'
