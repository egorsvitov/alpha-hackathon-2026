"""Backend-neutral ports for raw publication retrieval, shared by CPU and GPU."""
from typing import Any, Iterable, Protocol, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from llm.event_pipeline.company_news.contracts import CompanyQuery, RawNewsDocument


class RawDocumentIndexPort(Protocol):
    fingerprint: str
    snapshot_id: str
    documents: dict[str, str]

    def iter_blocks(self, max_rows: int) -> Iterable[tuple[Any, list[str]]]: ...


class PairRerankerPort(Protocol):
    fingerprint: str

    def rerank(self, company_query: "CompanyQuery", documents: Sequence["RawNewsDocument"]) -> list[dict]: ...
