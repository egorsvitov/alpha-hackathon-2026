"""Select time-valid B6 v0.2 snapshots without changing their claims."""
from ranking.b6 import require_valid_bundle
from ranking.contracts import parse_timestamp, payload_hash


def eligibility_reason(stored, *, as_of, window_start, window_end):
    b = stored.bundle
    if b.get('schema_version') != '0.2':
        return 'unsupported_contract_version'
    require_valid_bundle(b)
    event = b['event']
    if (event['event_id'], event['event_version'], payload_hash(b)) != (stored.event_id, stored.event_version, stored.payload_sha256):
        raise ValueError('snapshot_identity_mismatch')
    at, start, end = map(parse_timestamp, (as_of, window_start, window_end))
    if start > end or end > at:
        raise ValueError('invalid retrieval window')
    times = [stored.received_at, event['updated_at'], event.get('first_published_at')]
    times.extend(s.get('published_at') for s in b['sources'])
    if any(parse_timestamp(t) > at for t in times if t is not None):
        return 'future_snapshot_or_source'
    if event['availability'] != 'active':
        return 'event_unavailable'
    if event['quality'] == 'insufficient':
        return 'insufficient_evidence'
    if not b['claims'] or not b['evidence']:
        return 'missing_evidence'
    published = event.get('first_published_at')
    effective = parse_timestamp(published) if published else parse_timestamp(stored.received_at)
    return None if start <= effective <= end else 'outside_window'
