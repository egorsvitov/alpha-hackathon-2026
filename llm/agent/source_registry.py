"""Immutable-by-copy provenance; IDs cover versioned evidence coordinates."""
from copy import deepcopy
import hashlib
from ranking.contracts import payload_hash

def build_source_registry(snapshot, evidence_bundle=None):
    snap = snapshot.to_dict() if hasattr(snapshot, 'to_dict') else snapshot
    bundle = snap['bundle']
    articles = {s['source_id']: s for s in bundle['sources']}
    registry = {}
    for e in bundle['evidence']:
        s = articles[e['source_id']]
        sid = 'news:' + payload_hash([snap['event_id'], snap['event_version'], e['evidence_id']])
        registry[sid] = {'source_id': sid, 'type': 'synthetic_news' if s['is_synthetic'] else 'news',
            'locator': s.get('url') or s['source_id'], 'published_at': s['published_at'],
            'quote': e['quote'], 'verification_status': 'synthetic' if s['is_synthetic'] else 'pending_review',
            'publisher': s['publisher'], 'url': s['url'], 'content_kind': s['content_kind'],
            'is_synthetic': s['is_synthetic'], 'evidence_id': e['evidence_id'],
            'article_id': s['article_id'], 'article_version': s['article_version'],
            'event_id': snap['event_id'], 'event_version': snap['event_version'],
            'char_start': e['start'], 'char_end': e['end'], 'text_sha256': hashlib.sha256(e['quote'].encode('utf-8')).hexdigest()}
    for hit in (evidence_bundle or {}).get('hits', []):
        sid = 'bank:' + payload_hash([hit['document_sha256'],hit['chunk_id'],hit['char_start'],hit['char_end'],hit['text_sha256']])
        registry[sid] = {**deepcopy(hit), 'source_id': sid, 'type': 'bank_analytics',
            'locator': hit['source_path'], 'publisher': None, 'url': None, 'content_kind': 'bank_analytics',
            'corpus_version': evidence_bundle['corpus_version']}
    return deepcopy(registry)
