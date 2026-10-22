"""Explicit input to legacy analyst tests, not a candidate selector or ranker.

All caller-supplied features/claims are exposed for testing QA. A zero score is
only a required field of the old Relevance contract; it does not assess impact.
Never import this test helper in runtime code.
"""
from ranking.contracts import payload_hash, require_valid_contract


def analyst_relevance(snapshot, profile, as_of, role='cfo'):
    value = {
        'schema_version': '0.1', 'company_id': profile['company_id'],
        'profile_version': profile['profile_version'], 'user_role': role,
        'as_of': as_of.isoformat(), 'scoring_version': 'explicit-test-input-only',
        'rules_sha256': payload_hash({'fixture': 'explicit-analyst-input-v1'}),
        'event_id': snapshot.event_id, 'event_version': snapshot.event_version,
        'eligibility': 'eligible', 'reason_codes': [], 'score': 0, 'raw_total': 0,
        'scoring_diagnostics': {'groups': [], 'age_policy': 'decay', 'score_saturated': False},
        'level': 'low', 'reasons': [], 'candidate_channels': [],
        'feature_ids': sorted(profile['features']),
        'claim_ids': [c['claim_id'] for c in snapshot.bundle['claims']],
        'evidence_status': 'limited',
        'limitations': ['Explicit synthetic analyst input; no retrieval or relevance assessment performed.'],
    }
    require_valid_contract(value, 'relevance')
    return value
