"""Pure discovery, bounded plans, grounded compilation and semantic review for v2."""
import re
from typing import Union
from pydantic import create_model, ValidationError
from llm.events.contracts.models import Issue

from llm.events.extraction.protocol import digest, parse_extraction
from llm.events.extraction.transport import decode_stage_json
from llm.events.extraction.fragments import source_fragments
from llm.events.storage.store import StorageError, canonical_json, processing_hash
from llm.events.contracts.validation import article_sha256, pointer, _leaves
import llm.events.extraction.staged_models as m
import llm.events.extraction.prompts as prompts
from llm.events.extraction.references import fragments_with_spans, locate_span, wire_schema, INSTRUCTION

VERSION='two-stage.v2'
PROVENANCE=('two-stage.v2: проверены структура, буквальные основания и правила нормализации; '
            'это не доказательство смысловой поддержки каждого поля или истинности источника.')


def validate(model,raw):
    try:return model.model_validate(raw)
    except ValidationError as exc:
        raise StorageError('invalid_v2_schema','Invalid v2 payload',issues=[Issue(code='invalid_v2_schema',
            path='/'+'/'.join(map(str,e['loc'])),message=e['msg']) for e in exc.errors()]) from None


def locate(ref,record):
    if isinstance(ref,m.SpanRef) or isinstance(ref,dict) and "start_id" in ref:
        return locate_span(m.SpanRef.model_validate(ref),record)
    ref=m.QuoteRef.model_validate(ref)
    fragments={f['id']:f for f in source_fragments(record)}
    if ref.fragment_id not in fragments:raise StorageError('unknown_fragment',ref.fragment_id)
    f=fragments[ref.fragment_id];local=f['text'].find(ref.quote)
    if local<0:raise StorageError('quote_not_found','Quote absent in specified fragment')
    if f['text'].find(ref.quote,local+1)>=0:raise StorageError('quote_ambiguous','Quote repeated within specified fragment')
    return {'field':f['field'],'start':f['start']+local,'end':f['start']+local+len(ref.quote),'quote':ref.quote,
            'fragment_id':ref.fragment_id}


def parse_discovery(content,record,spec):
    raw,envelope=decode_stage_json(content)
    # Validate the envelope independently; one bad mention must not discard its peers.
    if not isinstance(raw,dict) or set(raw)!={'mentions','limitations'} or not isinstance(raw.get('mentions'),list):
        raise StorageError('invalid_v2_schema','Expected mentions and limitations')
    response=validate(m.DiscoveryResponse,{'mentions':[],'limitations':raw['limitations']})
    known={f['id'] for f in source_fragments(record)}
    mentions=[];rejected=[]
    for i,item in enumerate(raw['mentions']):
        mid=f'd{i+1}'
        try:
            mention=validate(m.Mention,item)
            if mention.primary_fragment_id not in known or not set(mention.context_fragment_ids)<=known:
                raise StorageError('unknown_fragment','Unknown discovery context')
            if mention.trigger.fragment_id!=mention.primary_fragment_id:
                raise StorageError('trigger_source_mismatch','Trigger must identify primary fragment')
            locate(mention.trigger,record)
            mentions.append(m.MentionRecord(**mention.model_dump(),mention_id=mid))
        except StorageError as exc:
            rejected.append(m.Coverage(mention_id=mid,status='rejected',issues=exc.issues))
    return m.DiscoveryResult(source_id=record.source_id,article_sha256=article_sha256(record.article),
        processing_spec_hash=processing_hash(spec),mentions=mentions,limitations=response.limitations,
        transport_envelope=envelope,rejected_mentions=rejected)


def validate_origin(payload,record):
    if payload.source_id!=record.source_id or payload.article_sha256!=article_sha256(record.article):
        raise StorageError('input_provenance_mismatch','Artifact does not describe archived revision')
    return payload


def context_for(record,mentions):
    fragments=source_fragments(record);known={f['id']:i for i,f in enumerate(fragments)}
    selected={'T1'}
    for mention in mentions:
        selected.add(mention.primary_fragment_id);selected.update(mention.context_fragment_ids)
        index=known[mention.primary_fragment_id]
        for i in (index-1,index+1):
            if 0<=i<len(fragments):selected.add(fragments[i]['id'])
        # Only pipe tables with an explicit header and fixed column count are interpreted.
        if fragments[index]['text'].lstrip().startswith('|'):
            lo=index
            while lo>0 and fragments[lo-1]['text'].lstrip().startswith('|'):lo-=1
            hi=index
            while hi+1<len(fragments) and fragments[hi+1]['text'].lstrip().startswith('|'):hi+=1
            def cells(f):return [s.strip() for s in f['text'].strip().strip('|').split('|')]
            header=fragments[lo];head=cells(header)
            if lo==index or not any(re.search(r'сентябрь|август|период|год|month|date',c,re.I) for c in head):
                raise StorageError('table_context_ambiguous','No explicit table header')
            if any(len(cells(fragments[i]))!=len(head) for i in range(lo,hi+1)):
                raise StorageError('table_context_ambiguous','Inconsistent table columns')
            selected.add(header['id'])
            if lo>0:selected.add(fragments[lo-1]['id'])
    return [f for f in fragments if f['id'] in selected]


def record_schema(allowed):
    classes=tuple(m.TEMPLATES[t] for t in allowed)
    union=classes[0] if len(classes)==1 else Union[classes]
    wire=create_model('BatchWire',__base__=m.ContractModel,
        records=(list[union],...),excluded_mentions=(list[m.Exclusion],...),limitations=(list[m.Text],...))
    return wire.model_json_schema()


def strict_schema(schema):
    """Provider-facing syntax only; parser still owns all semantic constraints."""
    if isinstance(schema,dict):
        result={k:strict_schema(v) for k,v in schema.items() if k not in ('default','discriminator')}
        if result.get('type')=='object' and 'properties' in result:
            result['required']=list(result['properties']);result['additionalProperties']=False
        return result
    if isinstance(schema,list):return [strict_schema(x) for x in schema]
    return schema


def request_for(stage,record,profile,*,mentions=None,batch=None,review=None):
    fragments=fragments_with_spans(record)
    if stage=='discover':
        prompt=prompts.DISCOVERY+'\n'+'\n'.join(k+': '+v for k,v in prompts.DEFINITIONS.items())
        schema=m.DiscoveryResponse.model_json_schema();data={}
    elif stage=='templates':
        prompt=prompts.EXTRACTION+'\n'+'\n'.join(k+': '+prompts.DEFINITIONS[k] for k in batch.allowed_templates)
        schema=record_schema(batch.allowed_templates)
        fragments=[f for f in fragments if f['id'] in batch.fragment_ids]
        data={'mentions':[x.model_dump(mode='json') for x in mentions if x.mention_id in batch.mention_ids]}
    else:
        prompt=prompts.VERIFICATION;schema=m.VerificationResponse.model_json_schema();data={'review':review}
    schema=wire_schema(schema)
    prompt+=INSTRUCTION
    data.update(reference_format='span-ids.v1',source=record.article.model_dump(mode='json',exclude={'text','title'}),
        fragments=[{'id':f['id'],'field':f['field'],'spans':[{'id':u['id'],'text':u['text']} for u in f['spans']]} for f in fragments])
    request={'model':profile.model,'messages':[{'role':'system','content':prompt+'\nJSON Schema:\n'+canonical_json(schema)},
        {'role':'user','content':'SOURCE_DATA_JSON\n'+canonical_json(data)}],'max_tokens':profile.max_tokens,'stream':False}
    if profile.response_format=='json_object':request['response_format']={'type':'json_object'}
    elif profile.response_format=='json_schema':request['response_format']={'type':'json_schema','json_schema':{
        'name':'financial_extraction_v2','strict':True,'schema':strict_schema(schema)}}
    if profile.thinking is not None:request['thinking']={'type':profile.thinking}
    if profile.reasoning_effort is not None:request['reasoning_effort']=profile.reasoning_effort
    return request


def plan_extraction(record,discovery,discovery_artifact_id,profile,configuration_sha256):
    discovery=validate_origin(validate(m.DiscoveryResult,discovery),record)
    groups={}
    for mention in discovery.mentions:groups.setdefault(mention.possible_templates[0],[]).append(mention)
    batches=[]
    def append(group,template):
        allowed=sorted({t for x in group for t in x.possible_templates})
        reason=None
        try:fragments=context_for(record,group)
        except StorageError as exc:fragments=[];reason=exc.code
        batch=m.Batch(batch_id='b'+digest([x.mention_id for x in group])[:20],template=template,
            allowed_templates=allowed,mention_ids=[x.mention_id for x in group],fragment_ids=[f['id'] for f in fragments],
            request_sha256='pending',request_bytes=0,deferred_reason=reason)
        request=request_for('templates',record,profile,mentions=group,batch=batch)
        size=len(canonical_json(request).encode())
        if not reason and size>profile.max_request_bytes and len(group)>1:
            mid=len(group)//2;append(group[:mid],template);append(group[mid:],template);return
        batch=batch.model_copy(update={'request_sha256':digest(request),'request_bytes':size,
            'deferred_reason':reason or ('input_too_large' if size>profile.max_request_bytes else None)})
        batches.append(batch)
    for template in sorted(groups):
        group=groups[template]
        for i in range(0,len(group),3):append(group[i:i+3],template)
    return m.ExtractionPlan(source_id=record.source_id,article_sha256=article_sha256(record.article),
        discovery_artifact_id=discovery_artifact_id,discovery_sha256=digest(discovery.model_dump(mode='json')),
        configuration_sha256=configuration_sha256,batches=batches)


def _unique_evidence(span,record):
    """Keep local binding coordinates; expand only the B3 quote if globally repeated."""
    source=getattr(record.article,span['field']);start,end=span['start'],span['end']
    while source.find(source[start:end])!=source.rfind(source[start:end]):
        start=max(0,start-1);end=min(len(source),end+1)
    quote=source[start:end];eid='e'+digest([span['field'],start,end])[:24]
    return {'evidence_id':eid,'field':span['field'],'quote':quote}


def compile_batch(content,record,discovery,batch):
    validate_origin(discovery,record)
    raw,envelope=decode_stage_json(content);response=validate(m.TemplateResponse,raw)
    mentions={x.mention_id:x for x in discovery.mentions if x.mention_id in batch.mention_ids}
    coverage={mid:m.Coverage(mention_id=mid,status='missing') for mid in batch.mention_ids}
    counts={};claims=[];evidence={};bindings=[];issues=[]
    excluded={}
    for x in response.excluded_mentions:
        if x.mention_id not in mentions:raise StorageError('unknown_mention','Excluded mention outside batch')
        excluded.setdefault(x.mention_id,[]).append(x)
    for mid,items in excluded.items():
        if len(items)!=1:raise StorageError('duplicate_exclusion',mid)
        coverage[mid]=m.Coverage(mention_id=mid,status='unresolved' if items[0].unresolved_type else 'excluded',
            issues=[Issue(code='unresolved_type' if items[0].unresolved_type else 'mention_excluded',path='/mentions/'+mid,message=items[0].reason)])
    for index,raw_record in enumerate(response.records):
        mid=raw_record.get('mention_id') if isinstance(raw_record,dict) else None
        try:
            fact=m.TEMPLATE.validate_python(raw_record)
            if fact.mention_id not in mentions:raise StorageError('unknown_mention','Record outside batch')
            if fact.mention_id in excluded:raise StorageError('mention_also_excluded','Record also excluded')
            if fact.template not in mentions[fact.mention_id].possible_templates:
                raise StorageError('template_not_proposed','Record changes type outside proposed alternatives')
            if fact.modality.value!='forecast' and fact.template in ('indicator_forecast','forecast_revision','decision_forecast','scenario_forecast'):
                raise StorageError('template_modality_mismatch','Forecast template requires forecast modality')
            if fact.template=='observation' and fact.modality.value not in ('asserted','opinion','unknown'):
                raise StorageError('template_modality_mismatch','Forecast/plan is not an observed measurement')
            if fact.template=='scenario_forecast' and fact.scenario.text.value.strip().casefold() in {'ждем','ждём','полагаем','вероятно','ожидаем'}:
                raise StorageError('scenario_is_modality','Modality cue alone is not a scenario')
            counts[mid]=counts.get(mid,0)+1;cid=mid+'r'+str(counts[mid]);refs={};local_evidence={};local_bindings=[]
            def value(bound,path,literal=False):
                if bound is None:return None
                for ref in bound.evidence:
                    if ref.fragment_id not in batch.fragment_ids:raise StorageError('evidence_outside_context',ref.fragment_id)
                    span=locate(ref,record)
                    if literal and bound.value not in '\n'.join(locate(r,record)['quote'] for r in bound.evidence):
                        raise StorageError('expression_not_quoted',str(bound.value),path)
                    e=_unique_evidence(span,record);local_evidence[e['evidence_id']]=e
                    if path!='/event_type':refs.setdefault(path,[]).append(e['evidence_id'])
                    local_bindings.append({'candidate_id':cid,'path':path,**span})
                return bound.value
            category=value(fact.event_type,'/event_type')
            periods={k:None for k in ('occurred_at','effective_at','observation_period','forecast_horizon')}
            def period(bound,key):
                if bound is not None:periods[key]={'text':value(bound,'/time/'+key+'/text',True)}
            if hasattr(fact,'horizon'):period(fact.horizon,'forecast_horizon')
            if fact.template=='observation':period(fact.period,'observation_period')
            if fact.template=='decision':period(fact.decision_time,'occurred_at');period(fact.effective_time,'effective_at')
            if fact.template=='action':
                period(fact.occurred_at,'occurred_at');period(fact.effective_at,'effective_at')
                period(fact.program_period,'observation_period');period(fact.plan_horizon,'forecast_horizon')
            stage=value(fact.stage,'/stage') if fact.template=='decision' else None
            scenario=None
            if fact.template=='scenario_forecast':
                scenario={'kind':value(fact.scenario.kind,'/scenario/kind'),'text':value(fact.scenario.text,'/scenario/text',True)}
            participants=[]
            if fact.subject:
                participants=[{'name':value(fact.subject,'/participants/0/name'),'role':'subject'}]
                refs['/participants/0/role']=refs['/participants/0/name']
            claim={'claim_id':cid,'text':value(fact.text,'/text'),'event_type':category,
                'modality':value(fact.modality,'/modality'),'polarity':value(fact.polarity,'/polarity'),'stage':stage,
                'attribution':{'name':value(fact.author,'/attribution/name')} if fact.author else None,
                'participants':participants,'action':value(fact.action,'/action'),'object':value(fact.object,'/object'),
                'geographies':[value(g,f'/geographies/{i}') for i,g in enumerate(fact.geographies)],
                'scenario':scenario,'time':periods,'measurements':[]}
            for j,measure in enumerate(fact.measurements):
                base=f'/measurements/{j}'
                data={name:value(getattr(measure,name),base+'/'+name,name in ('value_text','unit_text'))
                      for name in ('metric','value_text','unit_text','kind','qualifier','value_role')}
                comparison=value(measure.comparison_period_text,base+'/comparison_period/text',True)
                if comparison and re.fullmatch(r'[\s\d.,%+−–—/\-]+',comparison):raise StorageError('invalid_comparison_period','Numeric value is not a period')
                data.update(measurement_id=cid+'m'+str(j+1),comparison_period={'text':comparison} if comparison else None)
                claim['measurements'].append(data)
            claim.update(evidence_ids=sorted(local_evidence),field_evidence={p:sorted(set(v)) for p,v in refs.items()})
            validated=parse_extraction(canonical_json({'claims':[claim],'evidence':list(local_evidence.values()),'excluded':[],'limitations':[]}))
            claims.extend(c.model_dump(mode='json') for c in validated.claims);evidence.update(local_evidence);bindings.extend(local_bindings)
            coverage[mid].candidate_ids.append(cid);coverage[mid].status='processed'
        except (ValidationError,StorageError) as exc:
            errors=exc.issues if isinstance(exc,StorageError) else [Issue(code='invalid_template',path='/records/'+str(index)+'/'+'/'.join(map(str,e['loc'])),message=e['msg']) for e in exc.errors()]
            issues.extend(errors)
            if mid in coverage:
                coverage[mid].issues.extend(errors)
                if coverage[mid].status!='processed':coverage[mid].status='rejected'
    for c in coverage.values():
        if c.status=='missing':c.issues.append(Issue(code='mention_missing',path='/mentions/'+c.mention_id,message='No output record'))
    compiled=parse_extraction(canonical_json({'claims':claims,'evidence':[evidence[k] for k in sorted(evidence)],
        'excluded':[],'limitations':response.limitations+[PROVENANCE]}))
    return {'extraction':compiled.model_dump(mode='json'),'coverage':[x.model_dump(mode='json') for x in coverage.values()],
            'issues':[x.model_dump() for x in issues],'bindings':bindings,'transport_envelope':envelope}


def a_type(accepted):return accepted['event_type']


def required_paths(accepted):
    claim=accepted['claim'];paths=['/event_type']
    paths.extend(p for p in _leaves(claim) if p not in ('/source_id',) and not p.startswith('/field_evidence'))
    if not claim['participants']:paths.append('/participants')
    if claim['modality']=='forecast':
        if claim['attribution'] is None:paths.append('/attribution')
        if claim['time']['forecast_horizon'] is None:paths.append('/time/forecast_horizon')
    if claim['modality'] in ('asserted','proposal','plan') and a_type(accepted) in ('policy_rate','regulation','sanction_or_trade_restriction','corporate_action') and claim['stage'] is None:paths.append('/stage')
    if claim['modality']=='asserted' and claim['measurements'] and a_type(accepted) in ('market_indicator','company_results','fx_rate','price_change') and claim['time']['observation_period'] is None:paths.append('/time/observation_period')
    return sorted(set(paths))


def verification_input(accepted,normalized):
    return [{'candidate_id':a['candidate_id'],'event_type':a['event_type'],'claim':a['claim'],
             'required_paths':required_paths(a),'evidence':[e for e in normalized['evidence'] if e['evidence_id'] in a['claim']['evidence_ids']],
             'transformations':[t for t in normalized['transformations'] if t['candidate_id'] in (None,a['candidate_id'])]}
            for a in accepted]


def parse_verification(content,record,normalized_artifact_id,accepted):
    raw,envelope=decode_stage_json(content);response=validate(m.VerificationResponse,raw)
    expected={a['candidate_id']:a for a in accepted};group={}
    for verdict in response.verdicts:
        if verdict.candidate_id not in expected:raise StorageError('unknown_candidate','Unexpected verification candidate')
        group.setdefault(verdict.candidate_id,[]).append(verdict)
    selections=[]
    for cid,a in expected.items():
        issues=[];verdicts=group.get(cid,[]);fields={}
        if len(verdicts)!=1:issues.append(Issue(code='verification_missing_or_duplicate',path='/'+cid,message='Exactly one verdict required'))
        else:
            for field in verdicts[0].fields:fields.setdefault(field.path,[]).append(field)
        required=set(required_paths(a))
        if set(fields)-required:issues.append(Issue(code='unexpected_verification_path',path='/'+cid,message='Unknown or noncritical path'))
        for path in sorted(required):
            fs=fields.get(path,[])
            if len(fs)!=1:
                issues.append(Issue(code='verification_field_missing_or_duplicate',path=path,message='Exactly one field verdict required'));continue
            field=fs[0]
            if field.status!='supported':issues.append(Issue(code='semantic_'+field.status,path=path,message='Field not supported'))
            if field.status in ('supported','contradicted') and not field.evidence:
                issues.append(Issue(code='verification_evidence_missing',path=path,message='Verdict requires literal evidence'))
            for ref in field.evidence:
                try:locate(ref,record)
                except StorageError as exc:issues.extend(exc.issues)
            value=a['event_type'] if path=='/event_type' else pointer(a['claim'],path)
            if value is None or value==[] or (value=='unknown' and path in ('/modality','/polarity')):
                issues.append(Issue(code='critical_role_unknown',path=path,message='Unknown role cannot be approved'))
        selections.append(m.ClaimSelection(candidate_id=cid,accepted=not issues,issues=issues))
    return m.VerificationResult(source_id=record.source_id,article_sha256=article_sha256(record.article),
        normalized_artifact_id=normalized_artifact_id,selections=selections,response=response,transport_envelope=envelope)
