"""Semantic validation of analyst handoff, independent of technical retrieval scores."""
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker
from ranking.contracts import payload_hash, parse_timestamp, resolve_pointer
from .eligibility import eligibility_reason
from .representation_contracts import InvalidProfileReference,content_hash

SCHEMA_VERSION='impact_candidate_batch.v1'
ROLES={'ceo','cfo','treasurer','chief_accountant'}
_VALIDATOR=Draft202012Validator(json.loads((Path(__file__).parent/'contracts/impact-candidate-batch.v1.schema.json').read_text()),format_checker=FormatChecker())

def validate_candidate_batch(batch, *, profile, index, payment_context_source=None):
    _VALIDATOR.validate(batch)
    if batch['schema_version']!=SCHEMA_VERSION: raise ValueError('unsupported candidate contract')
    request=batch['request'];context=batch['company_context'];uid=profile['company_id']
    if request['company_id']!=uid or request['role'] not in ROLES: raise ValueError('request identity/role mismatch')
    if context['profile_hash']!=payload_hash(profile) or context['profile_version']!=profile['profile_version']:
        raise InvalidProfileReference('profile hash/version mismatch')
    for fact in context['verified_facts']:
        if fact != profile['features'].get(fact['feature_id']): raise InvalidProfileReference('foreign or changed company fact')
        if resolve_pointer(profile,fact['path'])!=fact['value']:raise InvalidProfileReference('unresolved company fact')
    if batch['retrieval_context']['index_snapshot_id']!=index.index_snapshot_id:raise ValueError('wrong index')
    if batch['retrieval_context']['encoder_fingerprint']!=index.fingerprint:raise ValueError('wrong encoder')
    for name in ('as_of','window_start','window_end'):
        if parse_timestamp(request[name])!=parse_timestamp(getattr(index,name)):raise ValueError('index/request cutoff mismatch')
    if bool(batch['candidates']) == (batch['empty_reason'] is not None):raise ValueError('empty_reason mismatch')
    if batch['status'] not in {'ready','empty','degraded'}: raise ValueError('invalid status')
    if (batch['status']=='empty') != (not batch['candidates']):raise ValueError('empty status mismatch')
    if len(batch['candidates'])>min(20,request['requested_limit']):raise ValueError('candidate limit exceeded')
    seen=set()
    payment=context.get('payment_context',{}).get('neighborhood',{})
    if payment:
        if not payment_context_source:raise InvalidProfileReference('payment context requires trusted snapshot source')
        source=payment_context_source['neighborhood']
        for key in ('company_id','dataset_manifest','graph_content_hash','provenance_hash','coverage'):
            if payment.get(key)!=source.get(key):raise InvalidProfileReference('payment metadata mismatch')
        for direction in ('incoming','outgoing'):
            trusted={n['edge_id']:n for n in source[direction]}
            for neighbor in payment.get(direction,[]):
                if neighbor!=trusted.get(neighbor['edge_id']):raise InvalidProfileReference('neighbor without verified edge')
        if context['payment_context']['full_context_ref']['sha256']!=content_hash(payment_context_source):raise InvalidProfileReference('payment artifact hash mismatch')
    neighbors={n['company_id']:n for d in ('incoming','outgoing') for n in payment.get(d,[])}
    for candidate in batch['candidates']:
        if len(candidate['members'])!=1 or candidate['event_cluster_id'] is not None:raise ValueError('grouping not enabled')
        member=candidate['members'][0];snap=member['snapshot'];stored=index.snapshots[snap['event_id']]
        if candidate['candidate_id']!=content_hash([stored.snapshot.to_dict(),'single-event.v1']):raise ValueError('candidate identity mismatch')
        if snap!=stored.snapshot.to_dict() or member['bundle']!=stored.bundle:raise ValueError('member differs from immutable source')
        if stored.event_id in seen:raise ValueError('duplicate event')
        seen.add(stored.event_id)
        if eligibility_reason(stored,**{k:request[k] for k in ('as_of','window_start','window_end')}):raise ValueError('ineligible event')
        match=candidate['match_context']
        for fid in match['feature_ids']:
            if fid not in profile['features']:raise InvalidProfileReference(fid)
        for ref in match.get('neighbor_feature_refs',[]):
            neighbor=neighbors.get(ref['company_id'])
            if not neighbor or ref not in neighbor['feature_refs']:raise InvalidProfileReference('unscoped neighbor reference')
        for ref in match['claim_refs']:
            if ref['event_id']!=stored.event_id or ref['event_version']!=stored.event_version or ref['claim_id'] not in {c['claim_id'] for c in stored.bundle['claims']}:
                raise ValueError('foreign claim reference')
        for path in match['event_paths']:resolve_pointer(stored.bundle,path)
        if match['hypotheses']:raise ValueError('retriever does not generate analytical hypotheses')
    return batch
