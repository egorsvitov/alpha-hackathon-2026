"""Validate a B5 record directly, without constructing a transport EventBundle."""
import re
from .catalog_models import CanonicalEventRecord
from .matching import measurement_scope
from .models import Issue
from .storage import canonical_json
from .validation import _parse, validate_claim_collection, pointer, ContractValidationError


def validate_catalog_record(payload,articles):
    result=_parse(CanonicalEventRecord,payload)
    validate_claim_collection(result.claims,result.sources,result.evidence,articles)
    claims={c.claim_id:c for c in result.claims};issues=[]
    def error(code,path,message):issues.append(Issue(code=code,path=path,message=message))
    seen=set()
    for i,conflict in enumerate(result.conflicts):
        if conflict.conflict_id in seen:error('duplicate_id',f'/conflicts/{i}','Duplicate conflict ID')
        seen.add(conflict.conflict_id);values=set();scopes=set();scenarios=set()
        for j,v in enumerate(conflict.variants):
            try:
                c=claims[v.claim_id];value=pointer(c.model_dump(mode='json'),v.field_path)
                values.add(canonical_json(value))
                match=re.fullmatch(r'/measurements/(0|[1-9][0-9]*)/value',v.field_path)
                if not match:raise ValueError('Expected a measurement value')
                scopes.add(canonical_json(measurement_scope(c.measurements[int(match[1])])) )
                scenarios.add(canonical_json(c.scenario.model_dump() if c.scenario else None))
            except (ValueError,KeyError,IndexError,TypeError):error('invalid_conflict_reference',f'/conflicts/{i}/variants/{j}','Unresolvable numeric field')
        if len(values)<2:error('identical_conflict_variants',f'/conflicts/{i}','Conflict needs distinct values')
        if len(scopes)>1 or len(scenarios)>1:error('different_conflict_scope',f'/conflicts/{i}','Conflict conditions differ')
    for i,r in enumerate(result.relations):
        if r.from_id==r.to_id:error('self_relation',f'/relations/{i}','A relation cannot target itself')
        if r.type=='related_stage':
            if r.target_kind!='event' or r.from_id!=result.event_id:error('invalid_relation_kind',f'/relations/{i}','Expected an outgoing event relation')
        elif r.target_kind!='claim' or r.from_id not in claims or (r.type=='duplicate_claim' and r.to_id not in claims):
            error('missing_claim',f'/relations/{i}','Relation references an unavailable current claim')
    if result.availability=='active' and not result.claims:error('empty_active_event','/claims','An active event needs current claims')
    if result.availability=='withdrawn' and (result.claims or result.evidence or result.sources):error('withdrawn_evidence','/claims','Withdrawn event must not publish current evidence')
    if issues:raise ContractValidationError(issues)
    return result
