"""Raw-document adapters for the shared lexical engine and bounded exact cosine."""
from collections import defaultdict
import numpy as np
from .channels.lexical import search_lexical


class RawLexicalIndex:
    def __init__(self, documents):
        self.documents = documents

    def search(self, text, top_k=50):
        return search_lexical(self, [text], top_k=top_k, id_field='document_id')


def exact_topk(queries, blocks, *, top_k=50):
    """Only query-batch × document-block allocated; stable ID tie breaking."""
    queries = np.asarray(queries, dtype=np.float32)
    if queries.ndim != 2 or not np.isfinite(queries).all() or not np.allclose(np.linalg.norm(queries, axis=1), 1, atol=1e-5):
        raise ValueError('normalized query matrix required')
    best = [[] for _ in queries]
    for ids, matrix in blocks:
        matrix = np.asarray(matrix, dtype=np.float32)
        if matrix.shape != (len(ids), queries.shape[1]) or not np.isfinite(matrix).all() or not np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-5):
            raise ValueError('incompatible normalized document matrix')
        scores = queries @ matrix.T
        for i, row in enumerate(scores):
            # Corpus blocks are bounded (default 512). Sorting avoids unstable argpartition ties.
            candidates = best[i] + [(uid, float(score)) for uid, score in zip(ids, row)]
            best[i] = sorted(candidates, key=lambda p: (-p[1], p[0]))[:top_k]
    return best


def family_fusion(hits, *, family_weights=None, channel_weights=None, k=60, union_cap=250, pool_cap=100):
    """Max RRF within each family/channel, never sum repeated query votes."""
    family_weights = family_weights or dict(facts=1., mechanisms=.5, hypotheses=.35, graph=.2)
    channel_weights = channel_weights or dict(dense=1., lexical=1.)
    if not 0 < pool_cap <= union_cap <= 250 or k <= 0:
        raise ValueError('invalid fusion limits')
    records = {}
    for hit in hits:
        r = records.setdefault(hit['document_id'], dict(document_id=hit['document_id'], hits=[], contributions={}))
        r['hits'].append(hit)
        key = hit['family'] + '/' + hit['channel']
        r['contributions'][key] = max(r['contributions'].get(key, 0.), 1 / (k + hit['rank']))
    for r in records.values():
        r['fusion_score'] = sum(value * family_weights[family] * channel_weights[channel]
                                for key, value in r['contributions'].items() for family, channel in [key.split('/')])
    ordered = sorted(records.values(), key=lambda r: (-r['fusion_score'], r['document_id']))
    for rank, r in enumerate(ordered, 1):
        r.update(fusion_rank=rank, exclusion_stage='retrieval' if rank > pool_cap else None,
                 exclusion_reason='union_cap' if rank > union_cap else 'pool_cap' if rank > pool_cap else None)
    return ordered
