"""Lossless source-claim windows; grouping and news deduplication are out of scope."""
import json
from .representation_contracts import ChunkRef, SnapshotKey, InputTooLong, content_hash

POLICY_VERSION = 'claims-char-windows-overlap80.v1'


def _windows(text, prefix, encoder):
    if encoder.token_lengths([prefix], input_kind='document')[0] >= 510:
        raise InputTooLong('claim metadata does not fit; no silent context truncation')
    start = 0
    while start < len(text):
        lo, hi = 1, len(text) - start
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if encoder.token_lengths([prefix + text[start:start+mid]], input_kind='document')[0] <= 512:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        if best == 0:
            raise InputTooLong('cannot fit claim character with context')
        end = start + best
        yield start, end, prefix + text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - min(80, best // 4))


def build_event_views(stored, *, encoder):
    b = stored.bundle
    key = SnapshotKey(stored.event_id, stored.event_version, stored.payload_sha256)
    title = b['event']['title']
    chunks, refs = [], []
    # Title itself may be long: preserve it in independent windows.
    for start, end, text in _windows(title, 'Заголовок: ', encoder):
        chunks.append(text)
        refs.append(ChunkRef(key, content_hash([key, 'title', start, end]), [], [{'path':'/event/title','start':start,'end':end}]))
    for i, claim in enumerate(b['claims']):
        metadata = {k:v for k,v in claim.items() if k not in {'text','claim_id','evidence_ids','field_evidence'}}
        prefix = 'Контекст утверждения: ' + json.dumps(metadata, ensure_ascii=False, sort_keys=True) + '\nУтверждение: '
        for start, end, text in _windows(claim['text'], prefix, encoder):
            chunks.append(text)
            refs.append(ChunkRef(key, content_hash([key, claim['claim_id'], start, end]),
                [{'event_id':key.event_id,'event_version':key.event_version,'claim_id':claim['claim_id']}],
                [{'path':f'/claims/{i}/text','start':start,'end':end}]))
    lexical = title + '\n' + '\n'.join(c['text'] for c in b['claims'])
    return lexical, chunks, refs
