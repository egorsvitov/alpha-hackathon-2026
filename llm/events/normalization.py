"""B4 deterministic normalization, partial publication and provenance checks."""
from copy import deepcopy
from pathlib import Path
import json
import re
from uuid import uuid5, NAMESPACE_URL
from pydantic import ValidationError
from .extraction import digest, parse_extraction
from .extraction_models import ExtractionResult
from .models import ArticleRecord, Source, SourceClaim, Evidence, Issue
from .normalization_models import (NormalizationConfig, NormalizedArticleResult,
    AcceptedClaim, RejectedClaim, Diagnostic, Transformation, CheckedExcluded)
from .normalization_dates import normalize_period
from .normalization_numbers import normalize_measurement
from .storage import StorageError, canonical_json
from .validation import article_sha256, validate_claim_collection, ContractValidationError, _covers, _parse, pointer

ROOT=Path(__file__).parent
CONDITIONS={'always':r'', 'russia_context':r'\b(?:росси\w*|рф)\b',
    'rshb_context':r'Россельхозбанк', 'sinara_bank_context':r'банк\w*\s+[«"„]?Синар\w*',
    'ras_context':r'\bРСБУ\b|российск\w*\s+стандарт\w*\s+бухгалтер',
    'ifrs_context':r'\bМСФО\b|\bIFRS\b|международн\w*\s+стандарт\w*'}


def normalization_schema():return NormalizedArticleResult.model_json_schema(mode='validation')


def resolved_dictionary(config):
    data=deepcopy(config.dictionary) if config.dictionary is not None else json.loads((ROOT/'normalization_vocabulary.json').read_text())
    if not isinstance(data,dict) or not isinstance(data.get('version'),str):raise StorageError('invalid_dictionary','Dictionary needs a version')
    for group in ('entity_aliases','metric_aliases'):
        if not isinstance(data.get(group),list):raise StorageError('invalid_dictionary','Alias lists are required')
        for entry in data[group]:
            if not isinstance(entry,dict) or entry.get('condition') not in CONDITIONS or not isinstance(entry.get('aliases'),list) or not entry['aliases'] or any(not isinstance(a,str) or not a for a in entry['aliases']):
                raise StorageError('invalid_dictionary','Alias must have machine-checkable conditions and nonempty names')
            if group=='entity_aliases' and (not isinstance(entry.get('entity_id'),str) or not entry['entity_id'].startswith('org:') or not isinstance(entry.get('canonical_name'),str)):
                raise StorageError('invalid_dictionary','Only explicit org: identifiers are supported')
            if group=='metric_aliases' and not isinstance(entry.get('canonical'),str):raise StorageError('invalid_dictionary','Metric canonical name required')
    return data


def configuration_hash(config):
    return digest({'rules_version':config.rules_version,'dictionary':resolved_dictionary(config),
        'rules_sha256':digest([ (ROOT/name).read_text() for name in
            ('normalization.py','normalization_dates.py','normalization_numbers.py','validation.py')]),
        'schema_sha256':digest(normalization_schema())})


def source_for(record):
    a=record.article
    return Source(source_id=record.source_id,article_id=a.article_id,article_version=record.article_version,
        title=a.title,publisher=a.source,url=a.url,published_at=a.published_at,content_kind=a.content_kind,
        is_full_original_text=a.is_full_original_text,is_synthetic=a.is_synthetic)


def _name(value):return ' '.join(value.strip().strip('«»"„“').casefold().split())


def alias(value,context,dictionary,group):
    entries=[e for e in dictionary[group] if _name(value) in {_name(a) for a in e['aliases']} and re.search(CONDITIONS[e['condition']],context,re.I)]
    ids={e['entity_id'] if group=='entity_aliases' else e['canonical'] for e in entries}
    if len(ids)==1:return next(iter(ids)),entries[0]
    return None,None


def exact_span(record,field,quote):
    text=getattr(record.article,field)
    first=text.find(quote)
    if first<0:raise StorageError('quote_not_found','Literal quotation is absent from the archived field')
    if text.find(quote,first+1)>=0:raise StorageError('quote_ambiguous','Quotation occurs more than once; supply a longer exact quotation')
    return first,first+len(quote)


def normalize_extraction(result,article,config=None):
    config=_parse(NormalizationConfig,config or NormalizationConfig())
    record=_parse(ArticleRecord,article)
    raw=_parse(ExtractionResult,result)
    if raw.source_id!=record.source_id or raw.article_sha256!=article_sha256(record.article):
        raise StorageError('source_version_mismatch','Input does not describe this exact archived record')
    extracted=parse_extraction(canonical_json(raw.extraction.model_dump(mode='json')))
    dictionary=resolved_dictionary(config);conf_hash=configuration_hash(config)
    def identifier(kind,local):return str(uuid5(NAMESPACE_URL,canonical_json(['alpha-events','b4',record.source_id,config.input_artifact_id,conf_hash,kind,local])))
    source=source_for(record);checked={};bad={};diagnostics=[];traces=[];accepted=[];rejected=[];excluded=[]
    def diagnostic(severity,cid,code,path,message):
        diagnostics.append(Diagnostic(severity=severity,candidate_id=cid,issue=Issue(code=code,path=path,message=message)))
    for i,e in enumerate(extracted.evidence):
        try:
            start,end=exact_span(record,e.field,e.quote)
            checked[e.evidence_id]=Evidence(evidence_id=identifier('evidence',e.evidence_id),source_id=record.source_id,field=e.field,start=start,end=end,quote=e.quote)
        except StorageError as exc:
            bad[e.evidence_id]=Issue(code=exc.code,path=f'/evidence/{i}',message=exc.issues[0].message)
            diagnostic('error',None,exc.code,f'/evidence/{i}',exc.issues[0].message)
    for i,candidate in enumerate(extracted.claims):
        cid=candidate.claim_id;base=f'/claims/{i}';candidate_traces=[]
        def support(path):
            return list(dict.fromkeys(eid for parent,ids in candidate.field_evidence.items() if _covers(parent,path) for eid in ids))
        def quotes(path):return [checked[e].quote for e in support(path)]
        def trace(path,rule,original,normalized,refs=None):
            candidate_traces.append(Transformation(candidate_id=cid,path=path,rule=rule,input_value=original,output_value=normalized,
                evidence_ids=[checked[e].evidence_id for e in support(path)],metadata_refs=refs or []))
        def warn(code,path,message=None):diagnostic('warning',cid,code,base+path,message or code)
        try:
            broken=[bad[e] for e in candidate.evidence_ids if e in bad]
            if broken:raise ContractValidationError(broken)
            data=candidate.model_dump(mode='json');data.pop('event_type')
            data['claim_id']=identifier('claim',cid)
            for field in ('attribution','participants'):
                values=([data[field]] if data[field] else []) if field=='attribution' else data[field]
                for j,entity in enumerate(values):
                    path='/attribution' if field=='attribution' else f'/participants/{j}'
                    context='\n'.join(quotes(path+'/name'))
                    eid,entry=alias(entity['name'],context,dictionary,'entity_aliases')
                    entity['entity_id']=eid
                    trace(path+'/name','entity_alias' if eid else 'entity_unresolved',entity['name'],
                        {'entity_id':eid,'canonical_name':entry['canonical_name'] if entry else None})
                    if eid is None:warn('entity_unresolved',path)
            period_info={}
            for field,period in candidate.time:
                if period is None:continue
                path='/time/'+field
                info=normalize_period(period.text,record.article.published_at,context='\n'.join(quotes(path+'/text')))
                if field!='observation_period':
                    info.warnings=[w for w in info.warnings if w!='calendar_year_not_complete']
                data['time'][field]=info.period;period_info[field]=info
                trace(path+'/text',info.rule,period.text,info.period,info.metadata_refs)
                for code in info.warnings:warn(code,path)
            for j,m in enumerate(candidate.measurements):
                path=f'/measurements/{j}'
                value,unit,detail=normalize_measurement(m,quotes(path+'/value_text'),quotes(path+'/unit_text'))
                context='\n'.join(quotes(path+'/metric'))
                metric,entry=alias(m.metric,context,dictionary,'metric_aliases')
                if metric is None:metric=m.metric;warn('metric_unresolved',path+'/metric')
                normalized={k:v for k,v in m.model_dump(mode='json').items() if k not in {'value_text','unit_text'}}
                normalized.update(measurement_id=identifier('measurement',m.measurement_id),metric=metric,value=value,unit=unit)
                trace(path+'/value_text',detail['rule'],{'value':m.value_text,'unit':m.unit_text,'multiplier':detail['multiplier']},{'value':value,'unit':unit})
                trace(path+'/metric','metric_alias' if entry else 'metric_preserved',m.metric,metric)
                if m.comparison_period:
                    obs=period_info.get('observation_period')
                    info=normalize_period(m.comparison_period.text,record.article.published_at,
                        context='\n'.join(quotes(path+'/comparison_period/text')),observation=obs.period if obs else None)
                    if obs and info.rule.startswith('comparison_'):
                        info.metadata_refs.extend(obs.metadata_refs)
                    normalized['comparison_period']=info.period
                    trace(path+'/comparison_period/text',info.rule,m.comparison_period.text,info.period,info.metadata_refs)
                    for code in info.warnings:warn(code,path+'/comparison_period')
                    if obs and obs.warnings and info.rule.startswith('comparison_'):
                        warn('comparison_inherits_date_assumption',path+'/comparison_period')
                data['measurements'][j]=normalized
            mapped={}
            for path,ids in candidate.field_evidence.items():
                if path=='/event_type':continue  # Classification metadata is outside SourceClaim.
                path=re.sub(r'(/measurements/\d+)/value_text$',r'\1/value',path)
                path=re.sub(r'(/measurements/\d+)/unit_text$',r'\1/unit',path)
                mapped[path]=[checked[e].evidence_id for e in ids]
                if path.endswith('/text') and ('/time/' in path or '/comparison_period/' in path):
                    parent=path.rsplit('/',1)[0]
                    # Only computed boundaries inherit the original expression's support.
                    mapped[parent+'/start']=mapped[path].copy();mapped[parent+'/end']=mapped[path].copy()
            data['evidence_ids']=[checked[e].evidence_id for e in candidate.evidence_ids]
            data['field_evidence']=mapped
            claim=SourceClaim.model_validate(data)
            validate_claim_collection([claim],[source],[checked[e] for e in candidate.evidence_ids],[record])
            accepted.append(AcceptedClaim(candidate_id=cid,event_type=candidate.event_type,claim=claim));traces.extend(candidate_traces)
        except (ContractValidationError,ValidationError) as exc:
            issues=exc.issues if isinstance(exc,ContractValidationError) else [Issue(code='invalid_normalized_claim',path=base,message='Normalized fields failed the B1 schema')]
            rejected.append(RejectedClaim(candidate_id=cid,issues=issues))
            for issue in issues:diagnostic('error',cid,issue.code,issue.path,issue.message)
    for i,item in enumerate(extracted.excluded):
        try:
            start,end=exact_span(record,item.field,item.quote)
            excluded.append(CheckedExcluded(field=item.field,start=start,end=end,quote=item.quote,reason=item.reason))
        except StorageError as exc:diagnostic('error',None,exc.code,f'/excluded/{i}',exc.issues[0].message)
    used={eid for a in accepted for eid in a.claim.evidence_ids}
    published_evidence=[e for e in checked.values() if e.evidence_id in used]
    quality='insufficient' if not accepted and (extracted.claims or diagnostics or rejected) else 'partial' if diagnostics or rejected else 'complete'
    limitations=list(dict.fromkeys(extracted.limitations+['Semantic support, factuality and extraction completeness are not independently assessed.']+[d.issue.code for d in diagnostics]))
    output=NormalizedArticleResult(source=source,input_artifact_id=config.input_artifact_id,article_sha256=raw.article_sha256,
        configuration_sha256=conf_hash,candidate_count=len(extracted.claims),accepted=accepted,evidence=published_evidence,
        rejected=rejected,excluded=excluded,diagnostics=diagnostics,limitations=limitations,transformations=traces,quality=quality)
    return validate_normalized_result(output,record)


def validate_normalized_result(result,article):
    output=_parse(NormalizedArticleResult,result);record=_parse(ArticleRecord,article)
    if output.article_sha256!=article_sha256(record.article) or output.source.source_id!=record.source_id:
        raise StorageError('source_version_mismatch','Normalized result does not match the archived article')
    validate_claim_collection([a.claim for a in output.accepted],[output.source],output.evidence,[record])
    ids=[a.candidate_id for a in output.accepted]+[r.candidate_id for r in output.rejected]
    if len(ids)!=len(set(ids)) or len(ids)!=output.candidate_count:
        raise StorageError('candidate_coverage_mismatch','Every input candidate must be accepted or explicitly rejected once')
    expected='insufficient' if not output.accepted and (output.candidate_count or output.diagnostics or output.rejected) else 'partial' if output.diagnostics or output.rejected else 'complete'
    if output.quality!=expected:raise StorageError('quality_mismatch','Quality is inconsistent with accepted candidates and diagnostics')
    evidence_ids={e.evidence_id for e in output.evidence}
    used={eid for a in output.accepted for eid in a.claim.evidence_ids}
    if evidence_ids!=used:raise StorageError('unused_evidence','Published evidence must be used by an accepted claim')
    for diagnostic in output.diagnostics:
        if diagnostic.candidate_id is not None and diagnostic.candidate_id not in ids:
            raise StorageError('invalid_diagnostic_reference','Diagnostic refers to an unknown candidate')
    for trace in output.transformations:
        if trace.candidate_id is not None and trace.candidate_id not in ids:
            raise StorageError('invalid_trace_reference','Transformation refers to an unknown candidate')
        if not set(trace.evidence_ids).issubset(evidence_ids):
            raise StorageError('invalid_trace_reference','Transformation refers to unpublished evidence')
        for ref in trace.metadata_refs:
            try:pointer(record.model_dump(mode='json'),ref)
            except (ValueError,KeyError,IndexError,TypeError):raise StorageError('invalid_trace_reference','Unknown archived metadata reference') from None
        if trace.rule=='nearest_period_year' and not any(d.candidate_id==trace.candidate_id and d.issue.code=='nearest_period_year' for d in output.diagnostics):
            raise StorageError('missing_date_assumption','Inferred year must retain an explicit warning')
    for e in output.excluded:
        if e.start<0 or e.end<=e.start or getattr(record.article,e.field)[e.start:e.end]!=e.quote:
            raise StorageError('quote_mismatch','Excluded fragment does not match archived text')
    return output
