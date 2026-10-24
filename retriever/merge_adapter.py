"""Thin synchronous bridge to the persistent cluster merge service.

``deadline_seconds`` gates new paid reservations. It is not a hard timeout:
``provider.complete`` must itself have a bounded transport timeout (the standard
OpenAIProvider uses 60 seconds). An in-flight call is always allowed to finish;
this adapter never abandons a paid request in a background thread.
"""
from __future__ import annotations

import math
import time

from llm.cluster_merge.core import configuration, digest, plan_merge, validate_merge_result
from llm.cluster_merge.models import Article, MergeRequest, Profile
from llm.cluster_merge.service import MergeError, MergeService

from .ports import MergeResolution


class _CountingLimiter:
    """Per invocation accounting around the caller's existing shared quota."""

    def __init__(self, limiter, deadline):
        self.limiter = limiter
        self.deadline = deadline
        self.calls = 0
        self.deadline_reached = False

    def reserve(self, key):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.deadline_reached = True
            raise TimeoutError('merge_deadline_reached')
        self.limiter.reserve(key)
        # Includes provider errors and failures after a successful reservation.
        self.calls += 1


class ClusterMergeAdapter:
    def __init__(self, service: MergeService, profile: Profile | None = None):
        self.service = service
        self.profile = Profile() if profile is None else Profile.model_validate(profile)
        sample = MergeRequest(cluster_id='_', revision='_', trigger_id='_',
                              articles=[Article(id='_', text='_')], profile=self.profile)
        self.configuration_fingerprint = digest(configuration(sample))

    def resolve(self, cluster, articles, trigger_id, *, max_calls, deadline_seconds=None):
        if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 0:
            raise ValueError('max_calls must be a nonnegative integer')
        if deadline_seconds is not None and (
            isinstance(deadline_seconds, bool) or not math.isfinite(deadline_seconds)
            or deadline_seconds < 0
        ):
            raise ValueError('deadline_seconds must be finite and nonnegative')
        articles = list(articles)
        ids = [a.revision_id for a in articles]
        if len(ids) != len(set(ids)) or set(ids) != set(cluster.member_revision_ids):
            raise ValueError('cluster_article_membership_mismatch')
        if trigger_id not in ids:
            raise ValueError('unknown_trigger')
        request = MergeRequest(
            cluster_id=cluster.cluster_id, revision=cluster.revision,
            trigger_id=trigger_id, profile=self.profile,
            articles=[Article(
                id=a.revision_id, text=a.text,
                title=a.title if a.title_origin == 'source' else '',
                published_at=a.published_at, source=a.source or None, url=a.url,
            ) for a in sorted(articles, key=lambda a: a.revision_id)],
        )
        plan = plan_merge(request)
        deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
        limiter = _CountingLimiter(self.service.limiter, deadline)
        # Never replace the limiter on the shared caller service: simultaneous
        # resolutions need independent counters while using the same quota/leases.
        scoped = MergeService(self.service.provider, limiter, self.service.path,
                              clock=self.service.clock)
        try:
            result = scoped.merge_cluster(request, max_calls=max_calls)
        except MergeError as exc:
            return MergeResolution(
                status='deferred' if exc.code == 'in_progress' else 'failed',
                operation_id=plan.operation_id, request=request,
                new_calls=limiter.calls, reason=exc.code,
            )
        except Exception:
            # Keep paid accounting even on unexpected storage/provider failures;
            # raw exception messages may contain provider credentials or content.
            return MergeResolution(status='failed', operation_id=plan.operation_id,
                                   request=request, new_calls=limiter.calls,
                                   reason='merge_service_failure')
        report = validate_merge_result(result, request)
        if not report.valid:
            return MergeResolution(status='failed', operation_id=plan.operation_id,
                                   request=request, new_calls=limiter.calls,
                                   reason='invalid_merge_result')
        reason = ('merge_deadline_reached' if limiter.deadline_reached else
                  ','.join(result.unresolved) or None)
        return MergeResolution(status=result.status, operation_id=result.operation_id,
                               result=result, request=request,
                               new_calls=limiter.calls, reason=reason)
