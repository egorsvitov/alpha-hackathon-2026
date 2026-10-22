"""Deterministic byte-bounded context with mandatory conflicting variants."""
from dataclasses import dataclass, field, asdict
from copy import deepcopy
from ranking.contracts import canonical_json, payload_hash, parse_timestamp, require_valid_contract
from ranking.b6 import validate_bundle

class ContextError(ValueError):
    pass

@dataclass(frozen=True)
class RunContext:
    run_id: str
    snapshot: dict
    profile: dict
    relevance: dict
    role: str
    as_of: str
    config: dict
    payload: dict
    excluded_artifacts: list
    fingerprint: str
    registry: dict = field(default_factory=dict)
    def to_dict(self):
        return asdict(self)

def build_context(snapshot, profile, relevance, *, run_id, role, as_of, config=None, registry=None):
    config = deepcopy(config or {})
    snap = snapshot.to_dict() if hasattr(snapshot, 'to_dict') else deepcopy(snapshot)
    at = parse_timestamp(as_of).isoformat()
    expected = {'event_id': snap['event_id'], 'event_version': snap['event_version'],
                'company_id': profile['company_id'], 'profile_version': profile['profile_version'],
                'user_role': role}
    for key, value in expected.items():
        if relevance.get(key) != value:
            raise ContextError('identity_mismatch:' + key)
    if parse_timestamp(relevance['as_of']) != parse_timestamp(at):
        raise ContextError('identity_mismatch:as_of')
    bundle = snap['bundle']
    issues = validate_bundle(bundle)
    if any(i.severity == 'error' for i in issues):
        raise ContextError('invalid_bundle')
    require_valid_contract(profile, 'company-profile')
    require_valid_contract(relevance, 'relevance')
    if snap['payload_sha256'] != payload_hash(bundle):
        raise ContextError('snapshot_hash_mismatch')
    if parse_timestamp(snap['received_at']) > parse_timestamp(at):
        raise ContextError('snapshot_not_yet_received')
    for timestamp in ('updated_at', 'first_published_at'):
        if bundle['event'].get(timestamp) and parse_timestamp(bundle['event'][timestamp]) > parse_timestamp(at):
            raise ContextError('producer_future_timestamp')
    if any(source['published_at'] is not None and parse_timestamp(source['published_at']) > parse_timestamp(at)
           for source in bundle['sources']):
        raise ContextError('source_future_timestamp')
    if bundle['event']['availability'] != 'active':
        raise ContextError('event_unavailable')
    if bundle['event']['quality'] == 'insufficient':
        raise ContextError('event_insufficient')
    if relevance['eligibility'] != 'eligible' or relevance['score'] is None:
        raise ContextError('relevance_not_eligible')
    if bundle['event']['event_id'] != snap['event_id'] or bundle['event']['event_version'] != snap['event_version']:
        raise ContextError('identity_mismatch:bundle')
    chosen = set(relevance.get('feature_ids', []))
    if not chosen.issubset(profile['features']):
        raise ContextError('unknown_feature_id')
    # All claims and conflicts are mandatory: never select a convenient variant.
    payload = {'identity': expected, 'as_of': at, 'claims': deepcopy(bundle['claims']),
               'conflicts': deepcopy(bundle['conflicts']),
               'features': {k: deepcopy(profile['features'][k]) for k in sorted(chosen)},
               'profile_limitations': deepcopy(profile['limitations']),
               'reasons': deepcopy(relevance['reasons']), 'limitations': deepcopy(relevance['limitations']),
               'bank_sources': [], 'news_sources': [deepcopy(x) for x in (registry or {}).values() if x['type'] != 'bank_analytics']}
    excluded = []
    max_bytes = config.get('max_context_bytes', 131072)
    for source in (registry or {}).values():
        if source['type'] != 'bank_analytics':
            continue
        if len(payload['bank_sources']) >= config.get('max_bank_hits', 5) or len(source['quote']) > config.get('max_quote_chars', 6000):
            excluded.append(source['source_id']); continue
        payload['bank_sources'].append(deepcopy(source))
    while len(canonical_json(payload).encode()) > max_bytes and payload['bank_sources']:
        excluded.append(payload['bank_sources'].pop()['source_id'])
    if len(canonical_json(payload).encode()) > max_bytes:
        raise ContextError('context_overflow')
    fingerprint = payload_hash({'snapshot': snap, 'profile': profile, 'relevance': relevance, 'config': config, 'payload': payload})
    return RunContext(run_id, snap, deepcopy(profile), deepcopy(relevance), role, at, config, payload, excluded, fingerprint, deepcopy(registry or {}))
