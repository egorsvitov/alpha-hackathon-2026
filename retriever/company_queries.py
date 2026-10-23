"""Cached company query vectors with explicit graph temporal applicability."""
from datetime import datetime, timezone
from ranking.contracts import parse_timestamp
from .representation_contracts import CompanyQuerySet, EncoderMismatch, QueryPolicy, GRAPH_VIEWS


def load_company_queries(snapshot, *, company_id, expected_encoder_fingerprint, policy, as_of):
    if snapshot.fingerprint != expected_encoder_fingerprint:
        raise EncoderMismatch('company snapshot and event index use different encoders')
    at=parse_timestamp(as_of)
    meta=snapshot.company_metadata(company_id)
    omitted=dict(meta.get('omitted',{})); limitations=list(meta.get('limitations',[]))
    enabled=list(policy.enabled_views)
    if policy.graph_enabled:
        enabled.extend('graph_'+d for d in policy.graph_policy.directions if 'graph_'+d not in enabled)
    else:
        enabled=[v for v in enabled if v not in GRAPH_VIEWS and v!='graph_combined']
    payment=meta.get('payment_context_ref')
    graph_available=True
    if payment or any(k.startswith('graph_') for k in enabled):
        manifest=(payment or {}).get('neighborhood',{}).get('dataset_manifest',{})
        if policy.temporal_mode=='strict':
            available=manifest.get('available_at');end=manifest.get('period_end')
            end_time=datetime.fromisoformat(end) if end else None
            if end_time is not None and end_time.tzinfo is None:end_time=end_time.replace(tzinfo=timezone.utc)
            graph_available=bool(available and end_time and parse_timestamp(available)<=at and end_time<=at)
            if not graph_available: limitations.append('graph_temporal_context_unavailable')
        else:
            limitations.append('static_context_diagnostic_not_historical_validation')
    queries=[]
    for item in snapshot.views(company_id, enabled_views=enabled):
        if item.encoder_fingerprint != expected_encoder_fingerprint:
            raise EncoderMismatch('view fingerprint differs from event index')
        if item.kind.startswith('graph_'):
            if not graph_available:
                omitted[item.kind]='graph_temporal_context_unavailable';continue
            if item.recipe_version!=policy.graph_policy.recipe_version or item.weight_policy!=policy.graph_policy.weight_policy:
                raise EncoderMismatch('graph recipe mismatch')
        queries.append(item)
    present={q.kind for q in queries}
    for kind in enabled:
        if kind not in present:omitted.setdefault(kind,'view_not_built_or_unavailable')
    return CompanyQuerySet(company_id,meta['profile_version'],snapshot.snapshot_id,queries,omitted,
        payment if graph_available else None,policy.temporal_mode,limitations)
