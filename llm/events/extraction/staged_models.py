"""Versioned internal wire/audit contracts; existing B1–B6 contracts stay unchanged."""
from typing import Annotated, Generic, Literal, TypeVar
from pydantic import Field, JsonValue, TypeAdapter, model_validator
from llm.events.contracts.models import ContractModel, Text, EventType, Modality, Issue
from llm.events.extraction.models import ExtractionResponse

TemplateKind = Literal['observation','indicator_forecast','forecast_revision','decision_forecast',
                       'scenario_forecast','decision','action']
T = TypeVar('T')

class QuoteRef(ContractModel):
    fragment_id: Text
    quote: Text

class SpanRef(ContractModel):
    fragment_id: Text
    start_id: Text
    end_id: Text

Reference = QuoteRef | SpanRef

class Bound(ContractModel, Generic[T]):
    value: T
    evidence: Annotated[list[Reference], Field(min_length=1)]

Surface = Bound[Text]

class Mention(ContractModel):
    primary_fragment_id: Text
    context_fragment_ids: list[Text] = Field(default_factory=list)
    label: Text
    trigger: Reference
    event_type: EventType
    possible_templates: Annotated[list[TemplateKind], Field(min_length=1,max_length=2)]

    @model_validator(mode='after')
    def distinct_templates(self):
        if len(set(self.possible_templates))!=len(self.possible_templates):raise ValueError('Duplicate possible templates')
        return self

class DiscoveryResponse(ContractModel):
    mentions: list[Mention]
    limitations: list[Text]

class MentionRecord(Mention):
    mention_id: Text

class DiscoveryResult(ContractModel):
    schema_version: Literal['discovery.v2'] = 'discovery.v2'
    source_id: Text
    article_sha256: Text
    processing_spec_hash: Text
    mentions: list[MentionRecord]
    limitations: list[Text]
    transport_envelope: Literal['json','markdown_json']
    rejected_mentions: list['Coverage'] = Field(default_factory=list)

class GroundedScenario(ContractModel):
    kind: Bound[Literal['baseline','alternative','unspecified']]
    text: Surface

class Measurement(ContractModel):
    metric: Surface
    value_text: Surface
    unit_text: Surface
    kind: Bound[Literal['level','absolute_change','relative_change']]
    qualifier: Bound[Literal['exact','approximate','gt','gte','lt','lte']]
    value_role: Bound[Literal['reported','before','after','comparison']]
    comparison_period_text: Surface | None = None

class RecordBase(ContractModel):
    mention_id: Text
    text: Surface
    event_type: Bound[EventType]
    modality: Bound[Modality]
    polarity: Bound[Literal['affirmed','negated','unknown']]
    subject: Surface | None = None
    author: Surface | None = None
    action: Surface | None = None
    object: Surface | None = None
    geographies: list[Surface] = Field(default_factory=list)
    measurements: list[Measurement] = Field(default_factory=list)

class Observation(RecordBase):
    template: Literal['observation']
    period: Surface | None

class IndicatorForecast(RecordBase):
    template: Literal['indicator_forecast']
    horizon: Surface | None

class ForecastRevision(RecordBase):
    template: Literal['forecast_revision']
    horizon: Surface | None

    @model_validator(mode='after')
    def revision_roles(self):
        if not {'before','after'} <= {m.value_role.value for m in self.measurements}:
            raise ValueError('A forecast revision requires before and after measurements')
        return self

class DecisionForecast(RecordBase):
    template: Literal['decision_forecast']
    horizon: Surface | None

class ScenarioForecast(RecordBase):
    template: Literal['scenario_forecast']
    horizon: Surface | None
    scenario: GroundedScenario

class Decision(RecordBase):
    template: Literal['decision']
    stage: Bound[Literal['announced','proposed','approved','effective','repealed']] | None
    decision_time: Surface | None = None
    effective_time: Surface | None = None

class Action(RecordBase):
    template: Literal['action']
    occurred_at: Surface | None = None
    effective_at: Surface | None = None
    program_period: Surface | None = None
    plan_horizon: Surface | None = None

Template = Annotated[Observation | IndicatorForecast | ForecastRevision | DecisionForecast |
                     ScenarioForecast | Decision | Action, Field(discriminator='template')]
TEMPLATE = TypeAdapter(Template)
TEMPLATES = {c.model_fields['template'].annotation.__args__[0]:c for c in
            (Observation,IndicatorForecast,ForecastRevision,DecisionForecast,ScenarioForecast,Decision,Action)}

class Exclusion(ContractModel):
    mention_id: Text
    reason: Text
    unresolved_type: bool = False

class TemplateResponse(ContractModel):
    records: list[JsonValue]
    excluded_mentions: list[Exclusion]
    limitations: list[Text]

class Batch(ContractModel):
    batch_id: Text
    template: TemplateKind
    allowed_templates: list[TemplateKind]
    mention_ids: list[Text]
    fragment_ids: list[Text]
    request_sha256: Text
    request_bytes: int
    deferred_reason: Text | None = None

class ExtractionPlan(ContractModel):
    schema_version: Literal['extraction-plan.v2'] = 'extraction-plan.v2'
    source_id: Text
    article_sha256: Text
    discovery_artifact_id: Text
    discovery_sha256: Text
    configuration_sha256: Text
    batches: list[Batch]

class Coverage(ContractModel):
    mention_id: Text
    status: Literal['processed','excluded','unresolved','rejected','deferred','missing']
    candidate_ids: list[Text] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

class ExtractionAudit(ContractModel):
    schema_version: Literal['extraction-audit.v2'] = 'extraction-audit.v2'
    source_id: Text
    article_sha256: Text
    variant: Literal['A','B']
    coverage: list[Coverage]
    issues: list[Issue]
    excluded_candidate_ids: list[Text] = Field(default_factory=list)
    input_artifact_ids: list[Text]
    incomplete: bool

class FieldVerdict(ContractModel):
    path: Text
    status: Literal['supported','contradicted','not_found','ambiguous']
    evidence: list[Reference]

class ClaimVerdict(ContractModel):
    candidate_id: Text
    fields: list[FieldVerdict]

class VerificationResponse(ContractModel):
    verdicts: list[ClaimVerdict]

class ClaimSelection(ContractModel):
    candidate_id: Text
    accepted: bool
    issues: list[Issue]

class VerificationResult(ContractModel):
    schema_version: Literal['semantic-review.v1'] = 'semantic-review.v1'
    source_id: Text
    article_sha256: Text
    normalized_artifact_id: Text
    selections: list[ClaimSelection]
    response: VerificationResponse
    transport_envelope: Literal['json','markdown_json']


class FieldBinding(ContractModel):
    candidate_id: Text
    path: Text
    field: Literal['title','text']
    start: Annotated[int,Field(ge=0)]
    end: Annotated[int,Field(gt=0)]
    quote: Text
    fragment_id: Text

class TemplateBatchResult(ContractModel):
    schema_version: Literal['template-batch.v2'] = 'template-batch.v2'
    source_id: Text
    article_sha256: Text
    processing_spec_hash: Text
    plan_artifact_id: Text
    batch_id: Text
    extraction: ExtractionResponse
    coverage: list[Coverage]
    issues: list[Issue]
    bindings: list[FieldBinding]
    transport_envelope: Literal['json','markdown_json']
