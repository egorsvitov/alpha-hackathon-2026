"""Source of truth for v0.2 JSON contracts; no extraction or entity resolution."""

from datetime import date, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def iso_date(value: str) -> str:
    date.fromisoformat(value)
    return value


def timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Expected ISO datetime with explicit timezone")
    return value


def uri(value: str) -> str:
    parts = urlsplit(value)
    if not parts.scheme or (parts.scheme in ("http", "https") and not parts.netloc):
        raise ValueError("Expected absolute URI")
    return value


def unique(values: list[str]) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError("Items must be unique")
    return values


Text = Annotated[str, Field(min_length=1)]
Date = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", json_schema_extra={"format": "date"}), AfterValidator(iso_date)]
Timestamp = Annotated[Text, AfterValidator(timestamp), Field(json_schema_extra={"format": "date-time"})]
URI = Annotated[Text, AfterValidator(uri), Field(json_schema_extra={"format": "uri"})]
DecimalText = Annotated[str, Field(pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")]
Pointer = Annotated[str, Field(pattern=r"^/")]
PositiveInt = Annotated[int, Field(ge=1)]
NonNegativeInt = Annotated[int, Field(ge=0)]
Labels = Annotated[list[Text], AfterValidator(unique), Field(json_schema_extra={"uniqueItems": True})]
ContentKind = Literal["original", "source_based_paraphrase", "synthetic", "user_provided", "unknown"]
EventType = Literal[
    "policy_rate", "fx_rate", "price_change", "regulation", "sanction_or_trade_restriction",
    "market_indicator", "company_results", "investment", "corporate_action",
    "operational_incident", "other",
]
Topic = Literal["rates", "sanctions", "fx", "trade", "inflation", "counterparty", "industry", "other"]
Modality = Literal["asserted", "forecast", "plan", "proposal", "opinion", "unknown"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")


class ArticleInput(ContractModel):
    article_id: Text
    source: Text | None
    url: URI | None
    title: Text
    published_at: Timestamp | None
    language: Text
    text: Text
    content_kind: ContentKind
    is_full_original_text: bool
    is_synthetic: bool = False


class ArticleRecord(ContractModel):
    article: ArticleInput
    article_version: PositiveInt
    source_id: Text


class Period(ContractModel):
    start: Date | None
    end: Date | None
    text: Text


class Entity(ContractModel):
    entity_id: Text | None
    name: Text


class Participant(Entity):
    role: Text


class ValueRange(ContractModel):
    min: DecimalText
    max: DecimalText


class Measurement(ContractModel):
    measurement_id: Text
    metric: Text
    kind: Literal["level", "absolute_change", "relative_change"]
    value: DecimalText | ValueRange
    unit: Text
    qualifier: Literal["exact", "approximate", "gt", "gte", "lt", "lte"]
    comparison_period: Period | None
    value_role: Literal["reported", "before", "after", "comparison"]


class Scenario(ContractModel):
    kind: Literal["baseline", "alternative", "unspecified"]
    text: Text


class ClaimTime(ContractModel):
    occurred_at: Period | None
    effective_at: Period | None
    observation_period: Period | None
    forecast_horizon: Period | None


class SourceClaim(ContractModel):
    claim_id: Text
    text: Text
    modality: Modality
    polarity: Literal["affirmed", "negated", "unknown"]
    stage: Literal["announced", "proposed", "approved", "effective", "repealed", "observed"] | None
    attribution: Entity | None
    participants: list[Participant]
    action: Text | None
    object: Text | None
    geographies: Labels
    time: ClaimTime
    measurements: list[Measurement]
    evidence_ids: Annotated[Labels, Field(min_length=1)]
    field_evidence: Annotated[dict[Pointer, Annotated[Labels, Field(min_length=1)]], Field(min_length=1)]
    scenario: Scenario | None


class Source(ContractModel):
    source_id: Text
    article_id: Text
    article_version: PositiveInt
    title: Text
    publisher: Text | None
    url: URI | None
    published_at: Timestamp | None
    content_kind: ContentKind
    is_full_original_text: bool
    is_synthetic: bool


class Evidence(ContractModel):
    evidence_id: Text
    source_id: Text
    field: Literal["title", "text"]
    start: NonNegativeInt
    end: PositiveInt
    quote: Text


class ConflictVariant(ContractModel):
    claim_id: Text
    field_path: Pointer


class Conflict(ContractModel):
    conflict_id: Text
    description: Text
    variants: Annotated[list[ConflictVariant], Field(min_length=2)]


class Relation(ContractModel):
    type: Literal["duplicate_claim", "updates", "related_stage"]
    from_id: Text
    to_id: Text
    target_kind: Literal["claim", "event"]


class Event(ContractModel):
    event_id: Text
    event_version: PositiveInt
    title: Text
    event_type: EventType
    first_published_at: Timestamp | None
    updated_at: Timestamp
    availability: Literal["active", "withdrawn"]
    quality: Literal["complete", "partial", "insufficient"]


class RankingFeatures(ContractModel):
    topics: Annotated[list[Topic], AfterValidator(unique), Field(json_schema_extra={"uniqueItems": True})]
    industries: Labels
    geographies: Labels
    objects: Labels
    metrics: Labels
    factors: Labels


class EventBundle(ContractModel):
    schema_version: Literal["0.2"]
    event: Event
    ranking_features: RankingFeatures
    claims: list[SourceClaim]
    sources: list[Source]
    evidence: list[Evidence]
    conflicts: list[Conflict]
    relations: list[Relation]
    limitations: Labels


class AnnotatedClaim(ContractModel):
    claim_id: Text
    event_type: EventType
    expected_event_id: Text | None
    identity_note: Text


class ExcludedFragment(ContractModel):
    quote: Text
    reason: Text


class ArticleAnnotation(ContractModel):
    article_id: Text
    article_version: PositiveInt
    article_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    claims: list[AnnotatedClaim]
    excluded: list[ExcludedFragment]
    limitations: list[Text]


class ExpectedLink(ContractModel):
    from_claim_id: Text
    to_claim_id: Text
    relation: Literal["duplicate_claim", "same_event", "conflicts_with", "updates", "different_event", "uncertain"]
    reason: Text


class GoldSet(ContractModel):
    schema_version: Literal["0.2"]
    annotation_version: Literal["b1.1"]
    annotation_status: Literal["single_annotator_draft"]
    dataset_kind: Literal["development_paraphrases", "synthetic_regression"]
    articles: list[ArticleRecord]
    annotations: list[ArticleAnnotation]
    bundles: list[EventBundle]
    expected_links: list[ExpectedLink]


class Issue(ContractModel):
    code: str
    path: str
    message: str
