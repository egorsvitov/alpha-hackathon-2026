"""B3 candidates. No normalized IDs, dates, coordinates or claims of grounding."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from urllib.parse import urlsplit
from llm.events.contracts.models import ContractModel, EventType, Modality, Text, Labels, Pointer, Scenario


class CandidatePeriod(ContractModel):
    text: Text


class CandidateTime(ContractModel):
    occurred_at: CandidatePeriod | None
    effective_at: CandidatePeriod | None
    observation_period: CandidatePeriod | None
    forecast_horizon: CandidatePeriod | None


class CandidateEntity(ContractModel):
    name: Text


class CandidateParticipant(CandidateEntity):
    role: Text


class CandidateMeasurement(ContractModel):
    measurement_id: Text
    metric: Text
    kind: Literal['level', 'absolute_change', 'relative_change']
    value_text: Text
    unit_text: Text
    qualifier: Literal['exact', 'approximate', 'gt', 'gte', 'lt', 'lte']
    value_role: Literal['reported', 'before', 'after', 'comparison']
    comparison_period: CandidatePeriod | None


class CandidateClaim(ContractModel):
    claim_id: Text
    text: Text
    event_type: EventType
    modality: Modality
    polarity: Literal['affirmed', 'negated', 'unknown']
    stage: Literal['announced', 'proposed', 'approved', 'effective', 'repealed', 'observed'] | None
    attribution: CandidateEntity | None
    participants: list[CandidateParticipant]
    action: Text | None
    object: Text | None
    geographies: Labels
    scenario: Scenario | None
    time: CandidateTime
    measurements: list[CandidateMeasurement]
    evidence_ids: Annotated[Labels, Field(min_length=1)]
    field_evidence: Annotated[dict[Pointer, Annotated[Labels, Field(min_length=1)]], Field(min_length=1)]


class CandidateEvidence(ContractModel):
    evidence_id: Text
    field: Literal['title', 'text']
    quote: Text


class CandidateExcluded(ContractModel):
    field: Literal['title', 'text']
    quote: Text
    reason: Text


class ExtractionResponse(ContractModel):
    claims: list[CandidateClaim]
    evidence: list[CandidateEvidence]
    excluded: list[CandidateExcluded]
    limitations: Labels


class ExtractionResult(ContractModel):
    schema_version: Literal['b3.1'] = 'b3.1'
    source_id: Text
    article_sha256: Text
    processing_spec_hash: Text
    verification_status: Literal['unverified'] = 'unverified'
    extraction: ExtractionResponse


class ProviderProfile(ContractModel):
    candidate_format: Literal['rows.v1', 'form.v1', 'compact.v1', 'b3.1'] = 'b3.1'
    execution_mode: Literal['live', 'offline'] = 'live'
    base_url: Text = 'https://changellenge.up.railway.app/v1'
    model: Text = 'deepseek/deepseek-v4.1-flash'
    max_tokens: Annotated[int, Field(gt=0)] = 4096
    # Whole serialized request size, not a claim of tokenizer equivalence.
    max_request_bytes: Annotated[int, Field(gt=0)] = 100000
    response_format: Literal['text', 'json_object', 'json_schema'] = 'text'
    format_verified: bool = False
    thinking: Literal['enabled', 'disabled'] | None = None
    thinking_verified: bool = False
    reasoning_effort: Literal['low', 'high', 'max'] | None = None
    connect_timeout: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 5.0
    read_timeout: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 60.0
    limits_confirmed: bool = False
    # Verified provider bounds, including chat framing; absent until confirmed.
    max_input_tokens: Annotated[int, Field(gt=0)] | None = None
    max_output_tokens: Annotated[int, Field(gt=0)] | None = None
    context_window_tokens: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode='after')
    def endpoint(self):
        url = urlsplit(self.base_url)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('base_url must be an HTTPS URL without credentials, query or fragment')
        return self


class ExecutionLimits(ContractModel):
    live_enabled: bool = False
    tariffs_confirmed: bool = False
    input_rub_per_million: str | None = None
    output_rub_per_million: str | None = None
    daily_quota_rub: str | None = None
    # These monetary settings do not affect the extraction cache key.


class RunOutcome(ContractModel):
    status: Literal['idle', 'succeeded', 'failed', 'deferred', 'superseded', 'cached']
    job_id: str | None = None
    call_id: str | None = None
    reason: str | None = None
    stop: bool = False
