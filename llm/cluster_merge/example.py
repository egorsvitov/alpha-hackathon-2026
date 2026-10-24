"""Executable integration contract: index mutation happens only after all embeddings."""

from copy import deepcopy

from .core import validate_merge_result


class ExampleIndex:
    def __init__(self, request):
        self.revision = request.revision
        self.cluster_id = request.cluster_id
        self.articles = deepcopy(
            request.articles
        )  # immutable archive survives replacement
        self.events = {}

    def apply(self, result, request, encode):
        if (
            result.status != "completed"
            or not validate_merge_result(result, request).valid
        ):
            raise ValueError("merge_not_applicable")
        # External model may fail: do not change the index yet.
        vectors = {e.id: encode(e.embedding_text) for e in result.events}
        # A real implementation MUST check and swap under its own transaction/lock.
        if (self.cluster_id, self.revision) != (
            result.expected_cluster_id,
            result.expected_revision,
        ):
            raise ValueError("cluster_changed")
        updated = {e.id: (e, vectors[e.id]) for e in result.events}
        self.events = updated
        self.revision += ":merged"
        return result.events[0].id if result.mode == "merged" else None
