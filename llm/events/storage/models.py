"""B2 state models, independent of the B1/B6 wire contracts."""

from typing import Annotated, Literal

from pydantic import Field, JsonValue

from llm.events.contracts.models import ArticleRecord, ContractModel, Issue, PositiveInt, Text, Timestamp

JobState = Literal["pending", "running", "succeeded", "failed", "deferred", "superseded"]


class ProcessingSpec(ContractModel):
    processor_name: Text
    processor_version: Text
    configuration: dict[str, JsonValue]


class BatchItem(ContractModel):
    index: Annotated[int, Field(ge=0)]
    article_id: Text | None
    source_id: Text | None
    previous_source_id: Text | None
    disposition: Literal["pending", "new", "updated", "unchanged", "historical", "invalid"]
    issues: list[Issue]


class BatchCounts(ContractModel):
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    historical: int = 0
    invalid: int = 0
    pending: int = 0


class ArticleChange(ContractModel):
    article_id: Text
    previous_source_id: Text | None
    source_id: Text


class BatchReceipt(ContractModel):
    batch_id: Text
    source_name: str | None
    received_at: Timestamp
    finished_at: Timestamp | None
    raw_sha256: Text
    import_status: Literal["importing", "completed", "partial", "rejected"]
    total_count: int | None
    counts: BatchCounts
    items: list[BatchItem]
    issues: list[Issue]
    current_changes: list[ArticleChange]


class VersionInfo(ContractModel):
    record: ArticleRecord
    article_sha256: Text
    received_at: Timestamp
    is_current: bool
    restored_from_source_id: Text | None
    restoration_reason: Text | None


class RestoreResult(ContractModel):
    record: ArticleRecord
    previous_source_id: Text
    restored_from_source_id: Text
    reason: Text


class AttemptRecord(ContractModel):
    attempt_id: Text
    job_id: Text
    attempt_number: PositiveInt
    token: Text
    worker_id: Text
    status: Literal["running", "succeeded", "failed", "deferred", "interrupted", "superseded"]
    started_at: Timestamp
    lease_expires_at: Timestamp
    finished_at: Timestamp | None
    issues: list[Issue]


class JobRecord(ContractModel):
    job_id: Text
    source_id: Text
    spec_hash: Text
    spec: ProcessingSpec
    state: JobState
    created_at: Timestamp
    updated_at: Timestamp
    is_current: bool
    reason: str | None
    result_artifact_id: Text | None
    active_attempt: AttemptRecord | None


class ArtifactRecord(ContractModel):
    artifact_id: Text
    attempt_id: Text
    kind: Text
    schema_version: Text | None
    payload: JsonValue
    created_at: Timestamp
