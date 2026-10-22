"""B6 internal publication contracts; EventBundle v0.2 stays unchanged."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from llm.events.contracts.models import ContractModel, Text, Labels, Topic, EventType, ArticleRecord, EventBundle, Issue, Timestamp, Pointer
from llm.events.catalog.models import PairDecision
from llm.events.normalization.models import NormalizedArticleResult


SHA256 = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]


class FeatureCondition(ContractModel):
    event_types: list[EventType] = Field(default_factory=list)
    objects: Labels = Field(default_factory=list)
    metrics: Labels = Field(default_factory=list)
    actions: Labels = Field(default_factory=list)

    @model_validator(mode='after')
    def nonempty(self):
        if not any((self.event_types,self.objects,self.metrics,self.actions)):
            raise ValueError('A feature rule needs an explicit condition')
        return self


class FeatureLabels(ContractModel):
    topics: list[Topic] = Field(default_factory=list)
    industries: Labels = Field(default_factory=list)
    factors: Labels = Field(default_factory=list)


class FeatureRule(ContractModel):
    rule_id: Text
    when: FeatureCondition
    labels: FeatureLabels


class FeatureDictionary(ContractModel):
    version: Text
    category_titles: dict[EventType, Text]
    subject_titles: dict[Text, Text]
    rules: list[FeatureRule]

    @model_validator(mode='after')
    def unique_rules(self):
        if len({r.rule_id for r in self.rules})!=len(self.rules):
            raise ValueError('Rule IDs must be unique')
        from typing import get_args
        if set(self.category_titles)!=set(get_args(EventType)):
            raise ValueError('Titles must cover every event category')
        return self


class PublicationConfig(ContractModel):
    rules_version: Literal['b6.1'] = 'b6.1'
    max_events: Annotated[int, Field(ge=1)] = 10000
    dictionary: FeatureDictionary | None = None


class NormalizedInput(ContractModel):
    artifact_id: Text
    result: NormalizedArticleResult


class PublicationContext(ContractModel):
    articles: list[ArticleRecord]
    normalized_inputs: list[NormalizedInput] = Field(default_factory=list)
    decisions: list[PairDecision] = Field(default_factory=list)
    missing_articles: Labels = Field(default_factory=list)
    catalog_claim_ids: Labels = Field(default_factory=list)


class FeatureBasis(ContractModel):
    claim_id: str | None
    path: Pointer
    evidence_ids: Labels
    classification: bool = False


class FeatureTrace(ContractModel):
    path: Pointer
    label: Text
    rule_id: Text
    bases: list[FeatureBasis]


class BundleProjection(ContractModel):
    schema_version: Literal['b6.1'] = 'b6.1'
    bundle: EventBundle
    feature_trace: list[FeatureTrace]
    quality_reasons: list[Issue]
    semantic_support: Literal['not_assessed'] = 'not_assessed'


class PublicationManifest(ContractModel):
    schema_version: Literal['b6.1'] = 'b6.1'
    catalog_id: Text
    publication_revision: Annotated[int, Field(ge=1)]
    catalog_revision: Annotated[int, Field(ge=0)]
    catalog_run_id: Text
    published_at: Timestamp
    configuration_sha256: SHA256
    bundle_schema_sha256: SHA256
    count: Annotated[int, Field(ge=0)]
    complete_snapshot: bool
    events_sha256: SHA256
