"""Versioned wire contracts, independent of the experimental pipelines."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Article(Model):
    id: str = Field(min_length=1)
    title: str = ""
    text: str = Field(min_length=1)
    published_at: str | None = None
    source: str | None = None
    url: str | None = None


class Profile(Model):
    model: str = "z-ai/glm-5.3-flash"
    endpoint: str = "https://changellenge.up.railway.app/v1"
    max_input_chars: int = Field(default=24000, ge=2000)
    max_output_tokens: int = Field(default=16384, gt=0)
    reasoning: bool = True


class MergeRequest(Model):
    format_version: Literal["cluster-merge.v1"] = "cluster-merge.v1"
    cluster_id: str
    revision: str
    articles: list[Article] = Field(min_length=1)
    trigger_id: str
    profile: Profile = Field(default_factory=Profile)
    configuration_version: str = "1"


class Fragment(Model):
    id: str
    article_id: str
    field: Literal["title", "text"]
    start: int
    end: int
    text: str


class Paragraph(Model):
    text: str = Field(min_length=1)
    fragment_ids: list[str] = Field(min_length=1)


class Draft(Model):
    title: str = Field(min_length=1)
    paragraphs: list[Paragraph] = Field(min_length=1)
    disagreements: list[Paragraph] = Field(default_factory=list)


class Disposition(Model):
    fragment_id: str
    kind: Literal["used", "duplicate", "service", "unresolved"]
    duplicate_of: str | None = None
    reason: str = ""


class Answer(Model):
    events: list[Draft]
    coverage: list[Disposition]


class Event(Model):
    id: str
    title: str
    text: str
    embedding_text: str
    article_ids: list[str]
    fragments: list[Fragment]
    disagreements: list[Paragraph]
    paragraphs: list[Paragraph]
    was_merged: bool
    publication_count: int
    source_count: int


class MergePlan(Model):
    operation_id: str
    input_hash: str
    fragments: list[Fragment]
    batches: list[list[str]]
    blocked: list[str]


class ValidationReport(Model):
    valid: bool
    errors: list[str]


class MergeResult(Model):
    format_version: Literal["cluster-merge.v1"] = "cluster-merge.v1"
    id: str
    operation_id: str
    input_hash: str
    configuration: dict
    status: Literal["completed", "partial", "deferred", "failed"]
    mode: Literal["merged", "split", "unchanged"] | None = None
    events: list[Event]
    coverage: list[Disposition]
    unresolved: list[str]
    calls: int
    usage: list[dict]
    cache_hit: bool = False
    expected_cluster_id: str
    expected_revision: str
    limitations: list[str] = Field(
        default_factory=lambda: ["Смысловая полнота текста автоматически не доказана."]
    )
