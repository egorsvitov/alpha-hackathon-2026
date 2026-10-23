"""Shared CPU/GPU contracts, owned by the GPU integration branch (v1)."""

from datetime import date, datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone_required")
    return parsed


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class NewsDocumentRef(Contract):
    document_id: str = Field(min_length=1)
    article_id: str | None = None
    article_version: str | None = None
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshot_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    published_at: str | None
    publication_precision: Literal["timestamp", "day", "unknown"]
    observed_at: str | None = None

    @model_validator(mode="after")
    def dates(self):
        if self.publication_precision == "unknown":
            if self.published_at is not None:
                raise ValueError("unknown_date_must_be_null")
        elif self.published_at is None:
            raise ValueError("missing_publication")
        elif self.publication_precision == "timestamp":
            instant(self.published_at)
        else:
            date.fromisoformat(self.published_at)
        if self.observed_at is not None:
            instant(self.observed_at)
        return self


class RawNewsDocument(Contract):
    reference: NewsDocumentRef
    title: str
    title_origin: Literal["source", "generated_identifier", "unknown"]
    text: str = Field(min_length=1)
    provenance: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def source_hash(self):
        if text_hash(self.text) != self.reference.content_hash:
            raise ValueError("source_content_hash_mismatch")
        return self


class SourceSpan(Contract):
    document_id: str
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    field: Literal["text"] = "text"
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    def resolve(self, documents):
        doc = documents[self.document_id]
        if self.content_hash != doc.reference.content_hash or not 0 <= self.start < self.end <= len(doc.text):
            raise ValueError("invalid_source_reference")
        return doc.text[self.start:self.end]


class CompanyQuery(Contract):
    query_id: str = Field(min_length=1)
    company_id: str
    profile_version: str
    text: str = Field(min_length=1)
    family: Literal["facts", "mechanisms", "hypotheses", "graph"]
    view: str
    anchor_feature_ids: list[str] = Field(min_length=1)
    template_version: str
    status: Literal["observed", "conditional", "hypothesis"]

    @model_validator(mode="after")
    def identity(self):
        if self.family == "hypotheses" and self.status != "hypothesis":
            raise ValueError("hypothesis_status_required")
        if self.family in ("mechanisms", "graph") and self.status == "observed":
            raise ValueError("inferred_query_is_not_observed")
        if self.query_id != fingerprint(self.model_dump(exclude={"query_id"})):
            raise ValueError("query_id_mismatch")
        return self


def company_query(**fields):
    return CompanyQuery(query_id=fingerprint(fields), **fields)


class QueryHit(Contract):
    document_id: str
    query_id: str
    family: Literal["facts", "mechanisms", "hypotheses", "graph"]
    channel: Literal["dense", "lexical"]
    rank: int = Field(gt=0)
    raw_score: float
    encoder_fingerprint: str | None = None


class CandidateTrace(Contract):
    document_id: str
    hits: list[QueryHit]
    fusion_score: float
    fusion_rank: int = Field(gt=0)
    rerank_score: float | None = None
    rerank_rank: int | None = None
    best_query_id: str | None = None
    reranker_status: Literal["not_requested", "scored", "unscored", "failed"] = "not_requested"
    selected: bool = False
    exclusion_stage: str | None = None
    exclusion_reason: str | None = None


class ContextSelection(Contract):
    document_id: str
    role: Literal["retrieved", "context_expansion"]
    parent_document_id: str | None = None
    group_ids: list[str] = Field(default_factory=list)
    spans: list[SourceSpan] = Field(min_length=1)

    @model_validator(mode="after")
    def provenance(self):
        if self.role == "context_expansion" and not self.parent_document_id:
            raise ValueError("context_expansion_parent_required")
        return self


class RawNewsCandidateBatch(Contract):
    schema_version: Literal["raw-news-candidate-batch.v1"] = "raw-news-candidate-batch.v1"
    company_id: str
    profile_version: str
    as_of: str
    snapshot_id: str
    queries: list[CompanyQuery]
    documents: list[RawNewsDocument]
    candidates: list[CandidateTrace]
    selected_context: list[ContextSelection] = Field(default_factory=list)
    budget: dict[str, Any]
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def references(self):
        instant(self.as_of)
        docs = {d.reference.document_id: d for d in self.documents}
        queries = {q.query_id: q for q in self.queries}
        if len(docs) != len(self.documents) or len(queries) != len(self.queries):
            raise ValueError("duplicate_document_or_query_id")
        if any(d.reference.snapshot_id != self.snapshot_id for d in self.documents):
            raise ValueError("snapshot_mismatch")
        if any(q.company_id != self.company_id or q.profile_version != self.profile_version for q in self.queries):
            raise ValueError("stale_company_query")
        for candidate in self.candidates:
            if candidate.document_id not in docs:
                raise ValueError("unknown_candidate_source")
            for hit in candidate.hits:
                if hit.document_id != candidate.document_id or hit.query_id not in queries or queries[hit.query_id].family != hit.family:
                    raise ValueError("unknown_or_mismatched_query_reference")
        for selection in self.selected_context:
            if selection.document_id not in docs or selection.parent_document_id and selection.parent_document_id not in docs:
                raise ValueError("unknown_context_source")
            for span in selection.spans:
                if span.document_id != selection.document_id:
                    raise ValueError("span_document_mismatch")
                span.resolve(docs)
        return self


class ExperimentManifest(Contract):
    schema_version: Literal["company-news-experiment.v1"] = "company-news-experiment.v1"
    experiment_id: str
    as_of: str
    snapshot_id: str
    inputs: dict[str, str]
    profile_versions: dict[str, str]
    code: dict[str, str]
    models: dict[str, dict[str, Any]]
    representations: dict[str, str]
    fusion: dict[str, Any]
    budget: dict[str, Any]
    split: dict[str, Any]
    state: str
    resources: dict[str, Any]

    @model_validator(mode="after")
    def date(self):
        instant(self.as_of)
        return self


class CompanyFact(Contract):
    feature_id: str
    value: Any
    status: Literal["observed", "derived", "unknown", "conflicting"]


class NewsAnalysisInput(Contract):
    schema_version: Literal["raw-news-analysis.v1"] = "raw-news-analysis.v1"
    batch: RawNewsCandidateBatch
    company_facts: list[CompanyFact]
    hypothesis_query_ids: list[str] = Field(default_factory=list)
    analytical_profile: str

    @model_validator(mode="after")
    def anchors(self):
        facts = {f.feature_id for f in self.company_facts}
        if len(facts) != len(self.company_facts):
            raise ValueError("duplicate_feature")
        for q in self.batch.queries:
            if not set(q.anchor_feature_ids) <= facts:
                raise ValueError("unknown_company_anchor")
        queries = {q.query_id: q for q in self.batch.queries}
        if any(q not in queries or queries[q].status != "hypothesis" for q in self.hypothesis_query_ids):
            raise ValueError("invalid_hypothesis_reference")
        return self
