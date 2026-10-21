"""B4 output: checked structure and literal spans, never a factuality verdict."""
from typing import Literal, Annotated
from pydantic import JsonValue, Field
from .models import ContractModel, Text, Issue, SourceClaim, Source, Evidence, EventType


class NormalizationConfig(ContractModel):
    rules_version: Literal['b4.1'] = 'b4.1'
    input_artifact_id: Text = 'pure-input'
    # Pure functions can receive explicit dictionaries; their content is hashed.
    dictionary: dict[str, JsonValue] | None = None


class AcceptedClaim(ContractModel):
    candidate_id: Text
    event_type: EventType
    claim: SourceClaim


class RejectedClaim(ContractModel):
    candidate_id: Text
    issues: list[Issue]


class Diagnostic(ContractModel):
    severity: Literal['warning','error']
    candidate_id: str | None
    issue: Issue


class Transformation(ContractModel):
    candidate_id: str | None
    path: Text
    rule: Text
    input_value: JsonValue
    output_value: JsonValue
    evidence_ids: list[str]
    metadata_refs: list[str]


class CheckedExcluded(ContractModel):
    field: Literal['title','text']
    start: Annotated[int, Field(ge=0)]
    end: Annotated[int, Field(gt=0)]
    quote: Text
    reason: Text


class NormalizedArticleResult(ContractModel):
    schema_version: Literal['b4.1'] = 'b4.1'
    source: Source
    input_artifact_id: Text
    article_sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    configuration_sha256: Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
    verification_status: Literal['structure_quotes_and_rules_checked'] = 'structure_quotes_and_rules_checked'
    semantic_support: Literal['not_assessed'] = 'not_assessed'
    candidate_count: Annotated[int, Field(ge=0)]
    accepted: list[AcceptedClaim]
    evidence: list[Evidence]
    rejected: list[RejectedClaim]
    excluded: list[CheckedExcluded]
    diagnostics: list[Diagnostic]
    limitations: list[str]
    transformations: list[Transformation]
    quality: Literal['complete','partial','insufficient']
