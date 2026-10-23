"""Exact, bounded-memory cosine search over an already active snapshot."""
import numpy as np

from .models import CompanyInterestSet, IndexSnapshot, SearchHit, text_hash


def exact_search(snapshot: IndexSnapshot, interests: CompanyInterestSet, *, include_graph=False,
                 block_size=512) -> list[SearchHit]:
    snapshot = IndexSnapshot.model_validate(snapshot.model_dump())
    interests = CompanyInterestSet.model_validate(interests.model_dump())
    if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < 1:
        raise ValueError('block_size_must_be_positive')
    if snapshot.encoder_fingerprint != interests.encoder_fingerprint:
        raise ValueError('encoder_fingerprint_mismatch')
    if snapshot.dimension != interests.dimension or snapshot.dimension < 1:
        raise ValueError('dimension_mismatch')
    items = {item.item_id: item for item in snapshot.items}
    if len(items) != len(snapshot.items):
        raise ValueError('duplicate_item_id')
    seen = set()
    represented = set()
    for row in snapshot.vectors:
        if row.vector_id in seen:
            raise ValueError('duplicate_vector_id')
        seen.add(row.vector_id)
        item = items.get(row.item_id)
        if item is None or row.item_version != item.item_version:
            raise ValueError('vector_item_version_mismatch')
        allowed = [text_hash(item.embedding_text)]
        if item.kind == 'article':
            proofs = item.derived_from.get('fragment_texts', {})
            for fragment_hash in item.derived_from.get('fragment_text_hashes', []):
                fragment = proofs.get(fragment_hash)
                if not isinstance(fragment, str) or not fragment or fragment not in item.text or text_hash(fragment) != fragment_hash:
                    raise ValueError('invalid_fragment_proof')
                allowed.append(fragment_hash)
        if row.embedding_text_sha256 not in allowed:
            raise ValueError('embedding_text_hash_mismatch')
        if len(row.values) != snapshot.dimension:
            raise ValueError('vector_dimension_mismatch')
        represented.add(row.item_id)
    if represented != set(items):
        raise ValueError('missing_item_vectors')
    active = sorted((i for i in interests.interests if i.active and
                     (include_graph or i.family != 'graph')), key=lambda i: i.interest_id)
    if not active or not items:
        return []
    queries = np.asarray([i.vector for i in active], dtype=np.float64)
    scores = {}
    for offset in range(0, len(snapshot.vectors), block_size):
        rows = snapshot.vectors[offset:offset + block_size]
        matrix = np.asarray([r.values for r in rows], dtype=np.float64)
        similarities = queries @ matrix.T
        for index, row in enumerate(rows):
            current = similarities[:, index]
            scores[row.item_id] = np.maximum(scores[row.item_id], current) if row.item_id in scores else current.copy()
    hits = []
    for item_id, values in scores.items():
        best = int(np.argmax(values))
        hits.append(SearchHit(item_id=item_id, item_version=items[item_id].item_version,
                              score=float(values[best]), best_interest_id=active[best].interest_id,
                              interest_scores={interest.interest_id: float(value)
                                               for interest, value in zip(active, values)}))
    return sorted(hits, key=lambda hit: (-hit.score, hit.item_id))
