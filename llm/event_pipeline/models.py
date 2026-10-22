"""Independent, permissive evidence-first contracts."""

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SourceBlock(Model):
    block_id: str
    document_id: str
    field: Literal["title", "text"]
    start: int
    end: int
    text: str


class EventMention(Model):
    mention_id: str
    document_id: str
    block_ids: list[str] = Field(min_length=1)
    context_block_ids: list[str] = Field(default_factory=list)
    gloss: str | None = None
    hints: dict[str, str | list[str]] = Field(default_factory=dict)
    diagnostics: list[str] = Field(default_factory=list)


class ExtractionAudit(Model):
    mentions: list[EventMention]
    rejected: list[dict]
    coverage: dict[str, str]


class MembershipDecision(Model):
    cluster_id: str
    identity: Literal["same", "different", "uncertain"]
    claim_compatibility: Literal["compatible", "conflicting", "unknown"]
    mention_ids: list[str] = Field(min_length=1)
    block_ids: list[str] = Field(min_length=1)
    reason: str


class EventCluster(Model):
    cluster_id: str
    version: int = 1
    mention_ids: list[str]
    decisions: list[MembershipDecision] = Field(default_factory=list)
    label: str
    label_kind: Literal["navigation"] = "navigation"
    verification_status: Literal[
        "assessed", "unassessed", "uncertain", "not_required"
    ] = "unassessed"


class RunManifest(Model):
    schema_version: Literal["event-pipeline.v1"] = "event-pipeline.v1"
    corpus_id: str
    document_ids: list[str]
    configuration: dict
    status: Literal["planned", "extracting", "clustering", "complete", "partial"] = (
        "planned"
    )
