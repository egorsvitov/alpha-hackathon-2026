"""Deterministic, explicitly experimental means of neighbor business queries."""
from dataclasses import replace
from decimal import Decimal
import math
import numpy as np
from .representation_contracts import (GraphViews, EncodedGraphView, DirectionCoverage, GraphPolicy,
    EncoderMismatch, CorruptVector, content_hash)
from .embedding_cache import vector_bytes, vector_sha256


def aggregate_graph_views(neighborhood, *, lookup, policy):
    if not policy.enabled:
        return GraphViews()
    views, omitted, coverages = {}, {}, {}
    for direction in policy.directions:
        neighbors = sorted(getattr(neighborhood, direction), key=lambda n: (n.company_id, n.edge_id))
        total = sum((Decimal(n.amount_raw) for n in neighbors), Decimal(0))
        if any(Decimal(n.amount_raw) <= 0 for n in neighbors):
            raise ValueError('retained graph edges must have positive amounts')
        inputs, fingerprint = [], None
        for n in neighbors:
            if n.profile_status != 'usable':
                continue
            item = lookup.get_business(n.company_id)
            if item is None:
                continue
            if item.kind != 'business' or item.company_id != n.company_id:
                raise CorruptVector('lookup returned wrong business identity')
            if item.profile_version != n.profile_version or item.view_content_hash != n.business_view_content_hash:
                raise CorruptVector('neighbor profile/business version mismatch')
            vector_bytes(item.vector)
            if vector_sha256(item.vector) != item.vector_sha256:
                raise CorruptVector('business checksum mismatch')
            if fingerprint is not None and fingerprint != item.encoder_fingerprint:
                raise EncoderMismatch('neighbor encoder fingerprint mismatch')
            fingerprint = item.encoder_fingerprint
            share = float(Decimal(n.amount_raw) / total)
            weight = 1. if policy.weight_policy == 'equal' else share if policy.weight_policy == 'amount_share' else math.sqrt(share)
            inputs.append((n, item, weight))
        amount = sum((Decimal(n.amount_raw) for n, _, _ in inputs), Decimal(0))
        original = neighborhood.coverage.get(direction, DirectionCoverage(retained_neighbor_count=len(neighbors), retained_amount_raw=str(total)))
        coverage = replace(original, business_vector_count=len(inputs), vector_backed_amount_raw=str(amount),
            vector_node_fraction=len(inputs) / len(neighbors) if neighbors else None,
            vector_amount_fraction=float(amount / total) if total else None)
        coverages[direction] = coverage
        kind = 'graph_' + direction
        if not inputs:
            omitted[kind] = 'no_business_vectors'
            continue
        denominator = math.fsum(w for _, _, w in inputs)
        refs, summed = [], np.zeros(384, dtype=np.float64)
        for n, item, weight in inputs:
            effective = weight / denominator
            summed += effective * item.vector.astype(np.float64)
            refs.append(dict(company_id=n.company_id, edge_id=n.edge_id, amount_raw=n.amount_raw,
                payment_count=n.payment_count, observed_share=n.observed_share, raw_weight=weight,
                normalized_weight=effective, vector_key=item.vector_key, vector_sha256=item.vector_sha256,
                profile_version=item.profile_version, feature_refs=[r.to_dict() for r in item.feature_refs]))
        norm = float(np.linalg.norm(summed))
        if norm < policy.near_zero_norm:
            omitted[kind] = 'near_zero_norm'
            continue
        vector = np.asarray(summed / norm, dtype=np.float32)
        recipe = dict(encoder_fingerprint=fingerprint, recipe=policy.recipe_version, direction=direction,
            weight_policy=policy.weight_policy, inputs=[dict(vector_key=r['vector_key'], weight=r['normalized_weight']) for r in refs])
        key = content_hash(recipe)
        semantic = content_hash(dict(recipe=recipe, graph_content_hash=neighborhood.graph_content_hash, coverage=coverage,
            inputs=refs, provenance_hash=neighborhood.provenance_hash))
        views[kind] = EncodedGraphView(company_id=neighborhood.company_id, kind=kind,
            view_content_hash=semantic, vector_key=key, encoder_fingerprint=fingerprint,
            vector_sha256=vector_sha256(vector), profile_version='', vector=vector,
            metadata_ref=dict(neighborhood=neighborhood.to_dict()), recipe_version=policy.recipe_version,
            direction=direction, weight_policy=policy.weight_policy, graph_content_hash=neighborhood.graph_content_hash,
            input_vector_refs=refs, coverage=coverage, normalized_weights_ref=refs,
            norm_before_normalization=norm, limitations=list(neighborhood.limitations) + ['experimental_neighbor_mean'])
    return GraphViews(views, omitted, coverages)
