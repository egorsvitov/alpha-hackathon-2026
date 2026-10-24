"""Injected infrastructure; no NLP or model weights needed for search."""
from dataclasses import dataclass
from typing import Any, Protocol
from .models import ClusterSnapshot


@dataclass(frozen=True)
class MergeResolution:
    status: str
    operation_id: str | None = None
    result: Any = None
    request: Any = None
    new_calls: int = 0
    reason: str | None = None


class EmbeddingProviderPort(Protocol):
    fingerprint: str
    dimension: int
    def encode_event_texts(self, texts: list[str]): ...


class MergePort(Protocol):
    configuration_fingerprint: str
    def resolve(self, cluster: ClusterSnapshot, articles, trigger_id: str, *, max_calls: int, deadline_seconds: float | None = None) -> MergeResolution: ...


class SearchStorePort(Protocol):
    def snapshot(self): ...
    def generation(self): ...
    def assert_merge_configuration(self, fingerprint): ...
    def get_sources(self, revision_ids): ...
    def clusters_for_item(self, item_id): ...
    def get_cluster_snapshot(self, cluster_id): ...
    def apply_merge(self, expected, result, encoded_items, *, request): ...
