from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, SerializationInfo, SerializerFunctionWrapHandler, model_serializer

Role = Literal["ceo", "cfo", "treasurer", "chief_accountant"]
SignificanceLevel = Literal["high", "medium", "low"]
Topic = Literal[
    "rates",
    "sanctions",
    "fx",
    "trade",
    "inflation",
    "counterparty",
    "industry",
    "other",
]
EvidenceType = Literal["news", "bank_research", "company_feature", "synthetic"]
ClaimKind = Literal["fact", "inference", "hypothesis"]


class ProductFlags(BaseModel):
    rko: bool | None = None
    guarantees: bool | None = None
    loans: bool | None = None
    leasing: bool | None = None
    deposits: bool | None = None
    payroll: bool | None = None
    factoring: bool | None = None

    model_config = ConfigDict(from_attributes=True)


class FtsInfo(BaseModel):
    flag: bool | None = None
    country: str | None = None

    model_config = ConfigDict(from_attributes=True)


class CounterpartyLink(BaseModel):
    name: str
    direction: Literal["in", "out"]
    share_pct: float
    note: str | None = None

    model_config = ConfigDict(from_attributes=True)


class CompanyProfile(BaseModel):
    company_id: str
    name: str
    initials: str | None = None
    okved_code: str = ""
    okved_name: str = ""
    industry: str
    stage: str = ""
    revenue_bn_rub: float
    age_label: str
    workers_label: str = ""
    region: str
    city: str = ""
    products: ProductFlags
    fts: FtsInfo
    group_id: str | None = None
    counterparties: list[CounterpartyLink] = Field(default_factory=list)
    feature_keys: list[str] = Field(default_factory=list)
    profile_version: str = "p-2026-10-20-a1"
    is_synthetic: Literal[True] = True

    model_config = ConfigDict(from_attributes=True)


class NewsEvent(BaseModel):
    event_id: str
    title: str
    date: str
    topic: Topic
    topic_label: str
    sources_count: int
    dedup_note: str | None = None
    is_synthetic: bool = False
    user_uploaded: bool | None = None
    synthetic_link: str | None = None

    model_config = ConfigDict(from_attributes=True)


class RelevanceFactor(BaseModel):
    feature_key: str
    label: str
    contribution: float
    detail: str
    feature_ids: list[str] = Field(default_factory=list)
    event_fields: list[str] = Field(default_factory=list)
    basis: str | None = None
    provenance: dict[str, Any] | None = None


class Relevance(BaseModel):
    score: float
    level: SignificanceLevel
    factors: list[RelevanceFactor] = Field(default_factory=list)
    explanation: str
    reasons: list[str] | None = None
    candidate_channels: list[str] = Field(default_factory=list)
    missing_context: list[str] = Field(default_factory=list)
    evidence_status: str | None = None


class FeedItem(BaseModel):
    event: NewsEvent
    relevance: Relevance
    why_short: str
    has_bank_position: bool
    profile_version: str


class Claim(BaseModel):
    text: str
    kind: ClaimKind | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    features: list[str] | None = None
    conditional: bool | None = None
    roles: list[Role] | None = None


class WhatHappened(BaseModel):
    summary: str
    facts: list[Claim] = Field(default_factory=list)


class Evidence(BaseModel):
    evidence_id: str
    type: EvidenceType
    title: str
    publisher: str
    date: str | None = None
    doc: str | None = None
    page: int | None = None
    url: str | None = None
    quote: str
    is_synthetic: bool = False
    # Необязательные координаты цитаты в тексте первоисточника (символы, [start, end)); фронт может не читать.
    start: int | None = None
    end: int | None = None


class UnknownItem(BaseModel):
    text: str
    how_to_clarify: str | None = None
    question: str | None = None


class TraceStep(BaseModel):
    n: int
    tool: str
    summary: str
    result: str
    status: Literal["ok", "retry", "warn"]


class ImpactEstimate(BaseModel):
    metric: str
    summary: str
    assumptions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class BankPosition(BaseModel):
    as_of: str | None = None
    claims: list[Claim] = Field(default_factory=list)


class AiConclusion(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)


class ChangesSincePrevious(BaseModel):
    """Что изменилось по сравнению с ранее сохранёнными выводами (память аналитика).

    Опциональное поле карточки: фронт может его игнорировать. Формат — как
    ``changes_since_previous`` в ``llm/agent/contracts/insight.schema.json``.
    """

    status: Literal["new", "continuation", "escalation", "deescalation", "unknown"]
    previous_insight_id: str | None = None
    summary: str | None = None
    claim_ids: list[str] = Field(default_factory=list)


class Insight(BaseModel):
    run_id: str
    company_id: str
    profile_version: str
    user_role: Role
    status: Literal["ok", "insufficient_data"]
    generated_by: Literal["live", "cached"]
    event: NewsEvent
    relevance: Relevance
    what_happened: WhatHappened
    why_it_matters: list[Claim] = Field(default_factory=list)
    risks: list[Claim] = Field(default_factory=list)
    opportunities: list[Claim] = Field(default_factory=list)
    next_actions: list[Claim] = Field(default_factory=list)
    impact_estimate: ImpactEstimate | None = None
    bank_position: BankPosition | None = None
    ai_conclusion: AiConclusion | None = None
    unknowns: list[UnknownItem] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    trace: list[TraceStep] = Field(default_factory=list)
    changes_since_previous: ChangesSincePrevious | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_changes(self, handler: SerializerFunctionWrapHandler, info: SerializationInfo):  # без аннотации: схема OpenAPI не меняется
        """Пустое ``changes_since_previous`` не попадает в JSON-ответ API: прежние ответы не меняются."""
        data = handler(self)
        if info.mode_is_json() and isinstance(data, dict) and data.get("changes_since_previous") is None:
            data.pop("changes_since_previous", None)
        return data


class QaRequest(BaseModel):
    company_id: str
    news_id: str
    role: Role
    run_id: str
    question: str


class QaAnswer(BaseModel):
    text: str
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    did_extra_search: bool = False
    extra_search_note: str | None = None
    from_cache: bool = False
    insufficient: bool = False


class NewsUploadRequest(BaseModel):
    title: str
    text: str = ""


class FeedbackRequest(BaseModel):
    run_id: str
    useful: bool
