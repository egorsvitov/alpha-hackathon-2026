"""Company-agnostic cached merge, query-local order and bounded candidate selection."""
from __future__ import annotations

from datetime import date
import math
import time
from typing import Callable

import numpy as np

from .models import (Candidate, EncodedItem, RetrievalCandidateBatch, RetrievalRun,
                     SearchItem, VectorRecord, canonical, digest, instant, text_hash)
from .search import exact_search


def source_available(article, request):
    at = instant(request.as_of)
    if article.observed_at and instant(article.observed_at) > at:
        return False
    if article.publication_precision == 'timestamp':
        return instant(article.published_at) <= at
    if article.publication_precision == 'day':
        return date.fromisoformat(article.published_at) <= at.date()
    return request.unknown_date_policy == 'include'


def source_in_window(article, request):
    if not source_available(article, request):
        return False
    if article.published_at is None:
        return request.unknown_date_policy == 'include'
    start = instant(request.window_start) if request.window_start else None
    end = instant(request.window_end or request.as_of)
    if article.publication_precision == 'day':
        day = date.fromisoformat(article.published_at)
        return (start is None or day >= start.date()) and day <= end.date()
    timestamp = instant(article.published_at)
    return (start is None or timestamp >= start) and timestamp <= end


def encoded_events(resolution, encoder, fingerprint, dimension):
    events = resolution.result.events
    if not events:
        return []
    if encoder is None:
        raise ValueError('event_encoder_required')
    if encoder.fingerprint != fingerprint or encoder.dimension != dimension:
        raise ValueError('event_encoder_profile_mismatch')
    vectors = np.asarray(encoder.encode_event_texts([e.embedding_text for e in events]))
    if vectors.shape != (len(events), dimension) or vectors.dtype != np.float32:
        raise ValueError('invalid_encoded_event_matrix')
    if not np.isfinite(vectors).all() or not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5, rtol=0):
        raise ValueError('invalid_encoded_event_norms')
    result = resolution.result
    out = []
    for event, vector in zip(events, vectors):
        item = SearchItem(item_id=event.id, item_version=digest(event.model_dump()), kind='event',
            title=event.title, text=event.text, embedding_text=event.embedding_text,
            source_revision_ids=event.article_ids, was_merged=event.was_merged,
            publication_count=event.publication_count, source_count=event.source_count,
            derived_from=dict(operation_id=result.operation_id, result_id=result.id,
                cluster_id=result.expected_cluster_id, cluster_revision=result.expected_revision,
                configuration_fingerprint=digest(result.configuration), event=event.model_dump()))
        v = VectorRecord(vector_id=digest([item.item_id, item.item_version, fingerprint]),
            item_id=item.item_id, item_version=item.item_version,
            embedding_text_sha256=text_hash(item.embedding_text), values=vector.tolist())
        out.append(EncodedItem(item=item, vectors=[v]))
    return out


class RetrieverService:
    def __init__(self, *, clock: Callable = time.monotonic):
        self.clock = clock

    def retrieve(self, request, *, interests, store, merge=None, encoder=None):
        from .models import RetrievalRequest, CompanyInterestSet
        request = RetrievalRequest.model_validate(request)
        interests = CompanyInterestSet.model_validate(interests)
        if (request.company_id, request.profile_version) != (interests.company_id, interests.profile_version):
            raise ValueError('company_profile_mismatch')
        began = self.clock()
        trace = dict(request=request.model_dump(), events=[], operations=[], generations=[],
                     calls_new=0, rebuilds=0, policy='max-cosine-lazy-merge.v1')
        trace_id = digest([request.model_dump(), interests.model_dump()])
        deferred = {}
        overrides = {}
        selected = []
        context_skipped = set()
        stop_reason = 'exhausted'
        forced_partial = False
        operation_count = 0
        last_generation = None
        snapshot = store.snapshot()
        if snapshot.corpus_id != request.corpus_id:
            raise ValueError('corpus_mismatch')
        if (snapshot.encoder_fingerprint, snapshot.dimension) != (interests.encoder_fingerprint, interests.dimension):
            raise ValueError('interest_index_profile_mismatch')

        def defer(cluster, reason, operation_id=None):
            key = (cluster.cluster_id, cluster.revision)
            if key not in deferred:
                deferred[key] = dict(cluster_id=key[0], revision=key[1], reason=reason, operation_id=operation_id)
            return key

        def batch(candidates, generation, reason):
            source_ids = sorted({i for c in candidates for i in c.item.source_revision_ids})
            sources = store.get_sources(source_ids)
            limitations = ['Search similarity is not business significance; final ranking belongs to the analyst',
                           'Unknown source/profile availability prevents a historical-validity claim']
            if any(a.publication_precision == 'day' for a in sources):
                limitations.append('Calendar dates compared in request calendar; no time or timezone invented')
            status = 'partial' if deferred or forced_partial else 'ready' if candidates else 'empty'
            return RetrievalCandidateBatch(request_id=request.request_id, company_id=request.company_id,
                profile_version=request.profile_version, interest_set_version=interests.interest_set_version,
                corpus_id=request.corpus_id, index_generation=generation,
                encoder_fingerprint=snapshot.encoder_fingerprint, as_of=request.as_of,
                requested_k=request.k, status=status, stop_reason=reason, candidates=candidates,
                sources=sources, deferred=list(deferred.values()), limitations=limitations, trace_id=trace_id)

        first_snapshot = True
        while True:
            if first_snapshot:
                first_snapshot = False
            else:
                snapshot = store.snapshot()
            stop_reason = 'exhausted'
            if merge is not None:
                store.assert_merge_configuration(merge.configuration_fingerprint)
            if last_generation is not None:
                trace['rebuilds'] += 1
                if trace['rebuilds'] > request.max_rebuilds:
                    selected = []  # Never emit stale selected versions after a failed refresh.
                    forced_partial = True
                    stop_reason = 'rebuild_limit'
                    break
            last_generation = snapshot.generation
            trace['generations'].append(last_generation)
            selected = []
            eligible = []
            for item in snapshot.items:
                articles = store.get_sources(item.source_revision_ids)
                if not all(source_available(a, request) for a in articles) or not any(source_in_window(a, request) for a in articles):
                    trace['events'].append(dict(item_id=item.item_id, reason='outside_availability_or_window'))
                    continue
                if item.kind == 'event' and merge is not None:
                    config = item.derived_from.get('configuration_fingerprint')
                    if config and config != merge.configuration_fingerprint:
                        raise ValueError('merge_configuration_changed_rebuild_index_explicitly')
                eligible.append(item)
            eligible_ids = {i.item_id for i in eligible}
            filtered = snapshot.model_copy(update=dict(items=eligible,
                vectors=[v for v in snapshot.vectors if v.item_id in eligible_ids]))
            hits = exact_search(filtered, interests, include_graph=request.include_graph)
            if not any(i.active and (request.include_graph or i.family != 'graph') for i in interests.interests):
                stop_reason = 'no_active_interests'
            items = {i.item_id: i for i in eligible}
            hits.sort(key=lambda h: (-overrides.get(h.item_id, (h.score,h.item_id))[0],
                                     overrides.get(h.item_id, (h.score,h.item_id))[1], h.item_id))
            restart = False
            for hit in hits:
                if self.clock() - began >= request.deadline_seconds:
                    forced_partial = True
                    stop_reason = 'deadline'
                    break
                item = items[hit.item_id]
                current = store.get_item(item.item_id)
                if current is None or current.item_version != item.item_version:
                    restart = True
                    break
                groups = store.clusters_for_item(item.item_id) if item.kind == 'article' else []
                need = [g for g in groups if g.status != 'resolved' and (len(g.member_revision_ids)>1 or g.status == 'ambiguous')]
                allow_raw = True
                if need:
                    ambiguous = len(need)>1 or any(g.status=='ambiguous' for g in need)
                    cluster = need[0]
                    key = (cluster.cluster_id,cluster.revision)
                    if ambiguous:
                        for g in need:
                            defer(g,'ambiguous_membership')
                        allow_raw = request.raw_fallback
                    elif key in deferred:
                        allow_raw = request.raw_fallback
                    elif merge is None:
                        defer(cluster,'merge_service_unavailable')
                        allow_raw = request.raw_fallback
                    elif operation_count >= request.max_merge_operations:
                        defer(cluster,'merge_operation_limit')
                        allow_raw = request.raw_fallback
                    else:
                        articles = store.get_sources(cluster.member_revision_ids)
                        if not all(source_available(a,request) for a in articles):
                            defer(cluster,'cluster_has_unavailable_sources')
                            allow_raw = request.raw_fallback
                        else:
                            operation_count += 1
                            try:
                                resolution = merge.resolve(cluster,articles,item.source_revision_ids[0],
                                    max_calls=request.max_new_llm_calls-trace['calls_new'],
                                    deadline_seconds=max(0.,request.deadline_seconds-(self.clock()-began)))
                            except Exception as exc:
                                # Do not retry a potentially paid/unknown call. Adapters must
                                # return known spending in MergeResolution instead of throwing.
                                defer(cluster,'merge_adapter_exception:'+type(exc).__name__)
                                allow_raw = request.raw_fallback
                            else:
                                trace['calls_new'] += resolution.new_calls
                                if resolution.new_calls < 0 or trace['calls_new'] > request.max_new_llm_calls:
                                    raise ValueError('merge_adapter_call_budget_violation')
                                trace['operations'].append(dict(cluster_id=cluster.cluster_id,
                                    revision=cluster.revision,status=resolution.status,
                                    operation_id=resolution.operation_id,new_calls=resolution.new_calls,reason=resolution.reason))
                                if resolution.status!='completed' or resolution.result is None:
                                    defer(cluster,resolution.reason or resolution.status,resolution.operation_id)
                                    allow_raw=request.raw_fallback
                                elif self.clock() - began >= request.deadline_seconds:
                                    defer(cluster,'deadline_after_merge_response',resolution.operation_id)
                                    allow_raw=request.raw_fallback
                                else:
                                    try:
                                        outputs = [] if resolution.result.mode=='unchanged' else encoded_events(
                                            resolution,encoder,snapshot.encoder_fingerprint,snapshot.dimension)
                                        if self.clock() - began >= request.deadline_seconds:
                                            raise TimeoutError('deadline_after_encoding')
                                        applied=store.apply_merge(cluster,resolution.result,outputs,request=resolution.request)
                                    except Exception as exc:
                                        defer(cluster,'merge_apply_error:'+type(exc).__name__,resolution.operation_id)
                                        allow_raw=request.raw_fallback
                                    else:
                                        if applied.stale:
                                            restart=True
                                            break
                                        if not applied.applied and not applied.already_applied:
                                            defer(cluster,applied.reason or 'merge_not_applied',resolution.operation_id)
                                            allow_raw=request.raw_fallback
                                        else:
                                            if resolution.result.mode=='merged' and len(applied.item_ids)==1:
                                                overrides[applied.item_ids[0]]=(hit.score,hit.item_id)
                                            restart=True
                                            break
                if not allow_raw:
                    continue
                candidate=Candidate(item=item,selection_score=overrides.get(item.item_id,(hit.score,))[0],
                    embedding_score=hit.score,score_origin='inherited_trigger' if item.item_id in overrides else 'cosine',
                    best_interest_id=hit.best_interest_id,interest_scores=hit.interest_scores,
                    group_ids=[g.cluster_id for g in groups],
                    limitations=['raw_fallback_may_include_duplicates'] if need else [])
                if need:
                    forced_partial=True
                trial=selected+[candidate]
                packet=batch(trial,snapshot.generation,'candidate_limit')
                if len(canonical(packet).encode()) > request.max_context_bytes:
                    context_skipped.add(item.item_id)
                    trace['events'].append(dict(item_id=item.item_id,reason='context_budget'))
                    continue
                selected=trial
                if len(selected)>=request.k:
                    stop_reason='candidate_limit'
                    break
            if restart:
                continue
            if store.generation() != snapshot.generation:
                continue
            if stop_reason=='exhausted' and context_skipped and len(selected)<request.k:
                stop_reason='context_budget'
                forced_partial=True
            break
        result=batch(selected,snapshot.generation,stop_reason)
        # Final status/deferred metadata may change the size after a provisional fit.
        while result.candidates and len(canonical(result).encode())>request.max_context_bytes:
            selected=selected[:-1]
            forced_partial=True
            stop_reason='context_budget'
            result=batch(selected,snapshot.generation,stop_reason)
        if len(canonical(result).encode())>request.max_context_bytes:
            # Even the metadata envelope cannot fit: explicitly reject the request.
            raise ValueError('context_budget_smaller_than_metadata_envelope')
        trace.update(elapsed_seconds=self.clock()-began,context_bytes=len(canonical(result).encode()),
                     returned=len(result.candidates),stop_reason=stop_reason)
        return RetrievalRun(batch=result,trace=trace)
