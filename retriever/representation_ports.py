"""Shared ports with no imports from implementations A, B or C."""
from __future__ import annotations
from typing import Protocol, Sequence, Any, Iterable
from .representation_contracts import EncodedView, ChunkRef, SnapshotKey

class EmbeddingEncoder(Protocol):
    fingerprint: str
    def encode_queries(self, texts: Sequence[str]) -> Any: ...
    def encode_documents(self, texts: Sequence[str]) -> Any: ...
    def token_lengths(self, texts: Sequence[str], *, input_kind: str) -> list[int]: ...

class BusinessVectorLookup(Protocol):
    def get_business(self, company_id: str) -> EncodedView | None: ...

class EventDenseIndexPort(Protocol):
    fingerprint: str
    index_snapshot_id: str
    as_of: str
    window_start: str
    window_end: str
    def iter_blocks(self, max_rows: int) -> Iterable[tuple[Any, list[ChunkRef]]]: ...
    def resolve_snapshot(self, ref: SnapshotKey) -> Any: ...

class RetrievalRunnerPort(Protocol):
    def run(self, request: dict, *, company_queries, config) -> dict: ...
