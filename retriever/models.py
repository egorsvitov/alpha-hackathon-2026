"""Standalone retrieval contracts; scores are query-local, never company facts."""
from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def canonical(value):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def text_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def instant(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if d.tzinfo is None or d.utcoffset() is None:
        raise ValueError('timezone_required')
    return d


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True, allow_inf_nan=False, revalidate_instances='always')


class ArticleRevision(Model):
    revision_id: str = Field(min_length=1)
    article_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    text_sha256: str = ''
    title: str = ''
    title_origin: Literal['source', 'generated_identifier', 'unknown'] = 'source'
    source: str = ''
    url: str | None = None
    language: str = 'und'
    published_at: str | None = None
    publication_precision: Literal['timestamp', 'day', 'unknown'] = 'unknown'
    observed_at: str | None = None
    provenance: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode='after')
    def valid(self):
        if not self.text.strip():
            raise ValueError('empty_article_text')
        expected = text_hash(self.text)
        if self.text_sha256 and self.text_sha256 != expected:
            raise ValueError('text_hash_mismatch')
        object.__setattr__(self, 'text_sha256', expected)
        if self.published_at is None:
            if self.publication_precision != 'unknown':
                raise ValueError('missing_publication')
        elif self.publication_precision == 'timestamp':
            instant(self.published_at)
        elif self.publication_precision == 'day':
            date.fromisoformat(self.published_at)
        else:
            raise ValueError('unknown_publication_must_be_null')
        if self.observed_at:
            instant(self.observed_at)
        return self


class SearchItem(Model):
    item_id: str = Field(min_length=1)
    item_version: str = '1'
    kind: Literal['article', 'event'] = 'article'
    title: str = ''
    text: str = Field(min_length=1)
    embedding_text: str = Field(min_length=1)
    source_revision_ids: list[str] = Field(min_length=1)
    derived_from: dict[str, Any] = Field(default_factory=dict)
    was_merged: bool = False
    publication_count: int = Field(default=1, ge=1)
    source_count: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def valid(self):
        if len(set(self.source_revision_ids)) != len(self.source_revision_ids):
            raise ValueError('duplicate_source_revision')
        return self


class VectorRecord(Model):
    vector_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    item_version: str = '1'
    embedding_text_sha256: str = Field(min_length=1)
    values: list[float] = Field(min_length=1)

    @model_validator(mode='after')
    def valid(self):
        if any(not math.isfinite(x) for x in self.values):
            raise ValueError('nonfinite_vector')
        if abs(math.sqrt(math.fsum(x*x for x in self.values)) - 1) > 1e-5:
            raise ValueError('unnormalized_vector')
        return self


class Interest(Model):
    interest_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    vector: list[float] = Field(min_length=1)
    family: Literal['activity', 'graph', 'other'] = 'activity'
    active: bool = True
    applicable_when: str | None = None
    applicability_basis: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid(self):
        if abs(math.sqrt(math.fsum(x*x for x in self.vector)) - 1) > 1e-5:
            raise ValueError('unnormalized_interest')
        return self


class CompanyInterestSet(Model):
    company_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    interest_set_version: str = Field(min_length=1)
    encoder_fingerprint: str = Field(min_length=1)
    dimension: int = Field(gt=0)
    interests: list[Interest]

    @model_validator(mode='after')
    def valid(self):
        if len({i.interest_id for i in self.interests}) != len(self.interests):
            raise ValueError('duplicate_interest_id')
        if any(len(i.vector) != self.dimension for i in self.interests):
            raise ValueError('interest_dimension_mismatch')
        return self


class ClusterSnapshot(Model):
    cluster_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    member_revision_ids: list[str] = Field(min_length=1)
    membership_hash: str = ''
    status: Literal['pending', 'resolved', 'ambiguous'] = 'pending'
    result_id: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def valid(self):
        if len(set(self.member_revision_ids)) != len(self.member_revision_ids):
            raise ValueError('duplicate_cluster_member')
        if self.member_revision_ids != sorted(self.member_revision_ids):
            raise ValueError('cluster_members_must_be_sorted')
        expected = digest(self.member_revision_ids)
        if self.membership_hash and self.membership_hash != expected:
            raise ValueError('cluster_membership_hash_mismatch')
        object.__setattr__(self, 'membership_hash', expected)
        return self


class IndexSnapshot(Model):
    corpus_id: str
    generation: int
    encoder_fingerprint: str
    dimension: int
    items: list[SearchItem]
    vectors: list[VectorRecord]


class SearchHit(Model):
    item_id: str
    item_version: str
    score: float
    best_interest_id: str
    interest_scores: dict[str, float]


class EncodedItem(Model):
    item: SearchItem
    vectors: list[VectorRecord] = Field(min_length=1)


class ApplyResult(Model):
    applied: bool = False
    stale: bool = False
    already_applied: bool = False
    generation: int
    item_ids: list[str] = Field(default_factory=list)
    reason: str | None = None


class RetrievalRequest(Model):
    request_id: str = Field(min_length=1)
    company_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    as_of: str
    k: int = Field(default=50, ge=1, le=10000)
    window_start: str | None = None
    window_end: str | None = None
    unknown_date_policy: Literal['include', 'exclude'] = 'include'
    include_graph: bool = False
    raw_fallback: bool = False
    max_new_llm_calls: int = Field(default=0, ge=0)
    max_merge_operations: int = Field(default=20, ge=0)
    max_rebuilds: int = Field(default=30, ge=0)
    deadline_seconds: float = Field(default=60., gt=0)
    max_context_bytes: int = Field(default=200000, gt=0)

    @model_validator(mode='after')
    def valid(self):
        at = instant(self.as_of)
        start = instant(self.window_start) if self.window_start else None
        end = instant(self.window_end) if self.window_end else at
        if end > at or (start and start > end):
            raise ValueError('invalid_retrieval_window')
        return self


class Candidate(Model):
    item: SearchItem
    selection_score: float
    embedding_score: float
    score_origin: Literal['cosine', 'inherited_trigger'] = 'cosine'
    best_interest_id: str
    interest_scores: dict[str, float]
    group_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RetrievalCandidateBatch(Model):
    schema_version: Literal['retrieval-candidate-batch.v1'] = 'retrieval-candidate-batch.v1'
    request_id: str
    company_id: str
    profile_version: str
    interest_set_version: str
    corpus_id: str
    index_generation: int
    encoder_fingerprint: str
    as_of: str
    requested_k: int
    status: Literal['ready', 'partial', 'empty']
    stop_reason: str
    candidates: list[Candidate]
    sources: list[ArticleRevision]
    deferred: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    trace_id: str


class RetrievalRun(Model):
    batch: RetrievalCandidateBatch
    trace: dict[str, Any]
