"""Контракты аналитика: ``AnalystInput.v1-draft``, ``AnalystOutput.v1-draft`` и черновик ответа LLM.

Схемы следуют ТЗ v1.1 (kb/sources/tz-llm-business-analyst-v1.1.md, разд. 3 и 5).
Допущения, принятые там, где ТЗ оставляет вопрос открытым:

* ``candidates[].bank_fragments`` — расширение входа: фрагменты аналитики банка,
  найденные Bank RAG для кандидата (в ТЗ Bank RAG — опциональный инструмент, P1).
  Пустой список — допустимый исход, позиция банка тогда ``not_found``.
* ``match_context.prior_score`` — балл отборщика (ранкера или шаблонного правила),
  только как сигнал «возможно интересно»; аналитик его не обязан подтверждать.
* В ответе модели (``LLMDraft``) используются короткие псевдонимы ID (``C1``, ``F1``,
  ``B1``): так дешевле и меньше ошибок копирования. В ``AnalystOutput`` возвращаются
  исходные ID — сопоставление делает валидатор (``qa.py``).
* Шкала ``priority_score`` 0..1 и пороги значимости — в ``analyst.priority_from_draft``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ANALYST_INPUT_VERSION = "AnalystInput.v1-draft"
ANALYST_OUTPUT_VERSION = "AnalystOutput.v1-draft"

Role = Literal["ceo", "cfo", "treasurer", "chief_accountant"]
ROLES: tuple[str, ...] = ("ceo", "cfo", "treasurer", "chief_accountant")
Decision = Literal["include", "watch", "exclude"]
ImpactDirection = Literal["positive", "negative", "mixed", "uncertain"]
InferenceStatus = Literal["supported", "partially_supported", "hypothesis", "insufficient_evidence"]
EventStatus = Literal["occurred", "forecast", "scenario", "refuted", "mixed", "unclear"]
Level = Literal["high", "medium", "low"]
StatementKind = Literal["fact", "inference", "hypothesis"]
BankStatus = Literal["found", "not_found", "outdated"]
# Совпадает с llm/agent/contracts/insight.schema.json → changes_since_previous.status.
ChangeStatus = Literal["new", "continuation", "escalation", "deescalation", "unknown"]
CHANGE_STATUSES: tuple[str, ...] = ("new", "continuation", "escalation", "deescalation", "unknown")
ImpactChannel = Literal[
    "demand_revenue",
    "costs",
    "credit_liquidity",
    "fx_trade",
    "logistics",
    "regulation",
    "taxes",
    "operations",
    "other",
]


class _Open(BaseModel):
    """Модели с ``extra=allow``: фрагменты EventBundle передаются без потерь."""

    model_config = ConfigDict(extra="allow")


# =========================================================================== вход


class Origin(BaseModel):
    type: str
    ref: str | None = None


class VerifiedFact(BaseModel):
    feature_id: str
    field: str
    value: Any = None
    description: str
    origin: Origin
    quality: Literal["observed", "derived", "demo_label", "unknown"] = "observed"


class EconomicExposure(BaseModel):
    exposure_id: str
    factor: str
    status: Literal["inferred", "confirmed"] = "inferred"
    confidence: Literal["low", "medium", "high"] = "low"
    basis_feature_ids: list[str] = Field(default_factory=list)
    description: str | None = None


class RelatedEntity(BaseModel):
    entity_id: str
    name: str
    relation: str
    share_pct: float | None = None
    basis_feature_ids: list[str] = Field(default_factory=list)
    note: str | None = None


class GraphContext(BaseModel):
    included: bool = False
    reason: str | None = None


class RetrievalInterest(BaseModel):
    """Интерес компании от ретривера: гипотеза о канале влияния, а не проверенный факт."""

    interest_id: str
    text: str
    version: str | None = None
    kind: Literal["hypothesis"] = "hypothesis"


class CompanyContext(BaseModel):
    profile_version: str
    profile_snapshot_ref: str | None = None
    verified_facts: list[VerifiedFact] = Field(default_factory=list)
    economic_exposures: list[EconomicExposure] = Field(default_factory=list)
    related_entities: list[RelatedEntity] = Field(default_factory=list)
    graph_context: GraphContext = Field(default_factory=GraphContext)
    unknowns: list[str] = Field(default_factory=list)
    synthetic_demo_profile: bool = True
    # Гипотезы ретривера о каналах влияния (отдельно от verified_facts и unknowns).
    retrieval_interests: list[RetrievalInterest] = Field(default_factory=list)


class EventBundleRef(BaseModel):
    event_id: str
    event_version: int = 1
    snapshot_sha256: str
    snapshot_ref: str | None = None


class ProjectionEvent(_Open):
    event_id: str
    event_type: str | None = None
    event_version: int = 1
    first_published_at: str | None = None
    quality: str | None = None
    title: str | None = None


class ProjectionClaim(_Open):
    claim_id: str
    text: str
    action: str | None = None
    object: str | None = None
    attribution: Any = None
    participants: list[Any] = Field(default_factory=list)
    geographies: list[Any] = Field(default_factory=list)
    measurements: list[Any] = Field(default_factory=list)
    modality: str | None = None
    polarity: str | None = None
    stage: str | None = None
    scenario: Any = None
    time: Any = None
    evidence_ids: list[str] = Field(default_factory=list)
    field_evidence: dict[str, Any] | None = None


class ProjectionEvidence(_Open):
    evidence_id: str
    quote: str
    source_id: str | None = None
    start: int | None = None
    end: int | None = None
    field: str | None = None


class ProjectionSource(_Open):
    source_id: str
    title: str | None = None
    publisher: str | None = None
    published_at: str | None = None
    url: str | None = None
    is_synthetic: bool | None = None


class ProjectionBundle(BaseModel):
    bundle_schema_version: str | None = None
    event: ProjectionEvent
    claims: list[ProjectionClaim] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[ProjectionSource] = Field(default_factory=list)
    evidence: list[ProjectionEvidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class AnalysisProjection(BaseModel):
    event_bundles: list[ProjectionBundle] = Field(default_factory=list)


class MatchSignal(BaseModel):
    channel: str
    profile_facet: str | None = None
    reason: str


class MatchContext(BaseModel):
    signals: list[MatchSignal] = Field(default_factory=list)
    matched_company_feature_ids: list[str] = Field(default_factory=list)
    matched_event_claim_ids: list[str] = Field(default_factory=list)
    confirmed_entity_match: bool | None = None
    impact_hypotheses: list[str] = Field(default_factory=list)
    assumptions_not_verified: list[str] = Field(default_factory=list)
    prior_score: float | None = None


class BankFragment(BaseModel):
    bank_chunk_id: str
    document_id: str | None = None
    title: str
    publisher: str = "Центр макроэкономических исследований Альфа-Банка"
    published_at: str | None = None
    page: int | None = None
    quote: str
    applicability: Literal["current", "outdated", "unknown"] = "unknown"
    is_synthetic: bool = False


class Candidate(BaseModel):
    candidate_id: str
    cluster_id: str
    event_bundle_refs: list[EventBundleRef]
    analysis_projection: AnalysisProjection
    match_context: MatchContext = Field(default_factory=MatchContext)
    bank_fragments: list[BankFragment] = Field(default_factory=list)
    # Флаги качества данных, выставленные адаптером (например, ``unreadable_claims`` —
    # текст утверждений повреждён ошибкой кодировки). Учитываются в ``precheck``.
    quality_flags: list[str] = Field(default_factory=list)
    # Идентичность события: группа похожих публикаций и итоговое (каноническое) событие.
    # Дубликат — только одинаковый ``canonical_event_id``; общий кластер/группа дублем не считается.
    reading_group_id: str | None = None
    canonical_event_id: str | None = None
    # Текст события — производный пересказ (а не дословный текст источника).
    derived_text: bool | None = None
    # Полнота кандидата: ``unresolved`` — принадлежность группы не подтверждена, анализируется
    # лучший переданный документ; ``partial`` — часть материалов группы не передана.
    completeness: Literal["complete", "unresolved", "partial"] | None = None
    group_member_refs: list[str | dict[str, Any]] = Field(default_factory=list)
    # Части контекста, не поместившиеся в бюджет (заполняет адаптер при ``over_budget``).
    omitted_parts: list[str] = Field(default_factory=list)


class AnalystRequest(BaseModel):
    request_id: str
    company_id: str
    role: Role
    as_of: str
    news_window_hours: int = 24 * 60
    max_insights: int = 10


class AnalysisPolicy(BaseModel):
    separate_facts_inferences_and_assumptions: bool = True
    require_supporting_claim_and_feature_ids: bool = True
    allow_exclude_and_insufficient_evidence: bool = True
    bank_rag_policy: str = "optional_tool_if_relevant"
    do_not_infer_supplier_from_payment: bool = True


class AnalystInput(BaseModel):
    schema_version: Literal["AnalystInput.v1-draft"] = ANALYST_INPUT_VERSION
    request: AnalystRequest
    company_context: CompanyContext
    candidates: list[Candidate] = Field(default_factory=list)
    analysis_policy: AnalysisPolicy = Field(default_factory=AnalysisPolicy)


# =========================================================================== черновик LLM


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, (str, dict)):
        return [v]
    return list(v)


class DraftStatement(BaseModel):
    """Утверждение в ответе модели. ID — псевдонимы ``C1``/``F1``/``B1``."""

    model_config = ConfigDict(extra="ignore")
    text: str
    kind: StatementKind = "inference"
    claim_ids: list[str] = Field(default_factory=list)
    feature_ids: list[str] = Field(default_factory=list)
    roles: list[Role] = Field(default_factory=list)
    # Алиасы исходных цитат (S1…) и интересов-гипотез (H1…); только для промптов analyst-v1.3+.
    quote_ids: list[str] = Field(default_factory=list)
    interest_ids: list[str] = Field(default_factory=list)

    @field_validator("claim_ids", "feature_ids", "roles", "quote_ids", "interest_ids", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> list[Any]:
        return [str(x).strip() for x in _as_list(v) if str(x).strip()]

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip().lower()
            return {"факт": "fact", "вывод": "inference", "гипотеза": "hypothesis", "assumption": "hypothesis"}.get(v, v)
        return v or "inference"

    @field_validator("roles", mode="after")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        return [r for r in v if r in ROLES]


class DraftUnknown(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str
    question: str | None = None


class DraftImpactEstimate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    metric: str
    scenario: str
    assumptions: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    feature_ids: list[str] = Field(default_factory=list)

    @field_validator("assumptions", "claim_ids", "feature_ids", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> list[Any]:
        return [str(x).strip() for x in _as_list(v) if str(x).strip()]


class DraftBank(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: BankStatus = "not_found"
    bank_chunk_ids: list[str] = Field(default_factory=list)

    @field_validator("bank_chunk_ids", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> list[Any]:
        return [str(x).strip() for x in _as_list(v) if str(x).strip()]


class DraftChanges(BaseModel):
    """«Что изменилось по сравнению с прошлым» в ответе модели (псевдонимы ``P1``, ``P1.1``, ``C1``)."""

    model_config = ConfigDict(extra="ignore")
    status: ChangeStatus = "unknown"
    previous_insight_id: str | None = None
    summary: str | None = None
    claim_ids: list[str] = Field(default_factory=list)

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v: Any) -> Any:
        if not isinstance(v, str):
            return "unknown"
        v = v.strip().lower()
        v = {"новое": "new", "продолжение": "continuation", "эскалация": "escalation", "усиление": "escalation",
             "ослабление": "deescalation", "de-escalation": "deescalation", "неизвестно": "unknown"}.get(v, v)
        return v if v in CHANGE_STATUSES else "unknown"

    @field_validator("previous_insight_id", "summary", mode="before")
    @classmethod
    def _opt_str(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none")):
            return None
        return str(v).strip()

    @field_validator("claim_ids", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> list[Any]:
        return [str(x).strip() for x in _as_list(v) if str(x).strip()]


class LLMDraft(BaseModel):
    """Что возвращает модель на одного кандидата (до проверки QA)."""

    model_config = ConfigDict(extra="ignore")
    decision: Decision
    exclude_reason: str | None = None
    event_status: EventStatus = "unclear"
    impact_direction: ImpactDirection = "uncertain"
    impact_channels: list[ImpactChannel] = Field(default_factory=list)
    materiality: Level = "low"
    urgency: Level = "low"
    headline: str
    what_happened: list[DraftStatement] = Field(default_factory=list)
    company_relevance: DraftStatement | None = None
    impact_mechanism: DraftStatement | None = None
    risks: list[DraftStatement] = Field(default_factory=list)
    opportunities: list[DraftStatement] = Field(default_factory=list)
    next_steps: list[DraftStatement] = Field(default_factory=list)
    recommended_checks: list[str] = Field(default_factory=list)
    unknowns: list[DraftUnknown] = Field(default_factory=list)
    impact_estimate: DraftImpactEstimate | None = None
    bank_assessment: DraftBank = Field(default_factory=DraftBank)
    ai_conclusion: DraftStatement | None = None
    inference_status: InferenceStatus = "hypothesis"
    changes_since_previous: DraftChanges | None = None

    @field_validator("changes_since_previous", mode="before")
    @classmethod
    def _changes(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"status": v}
        return v if isinstance(v, dict) else None

    @field_validator("decision", "event_status", "impact_direction", "materiality", "urgency", "inference_status", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("impact_channels", mode="before")
    @classmethod
    def _channels(cls, v: Any) -> list[str]:
        allowed = set(ImpactChannel.__args__)  # type: ignore[attr-defined]
        return [str(x).strip().lower() for x in _as_list(v) if str(x).strip().lower() in allowed]

    @field_validator("what_happened", "risks", "opportunities", "next_steps", mode="before")
    @classmethod
    def _stmts(cls, v: Any) -> list[Any]:
        out = [{"text": x} if isinstance(x, str) else x for x in _as_list(v)]
        # Пустые заготовки ({"text": null}) — не ошибка схемы, а отсутствие пункта.
        return [x for x in out if not (isinstance(x, dict) and not str(x.get("text") or "").strip())]

    @field_validator("company_relevance", "impact_mechanism", "ai_conclusion", mode="before")
    @classmethod
    def _stmt(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"text": v} if v.strip() else None
        if isinstance(v, dict) and not str(v.get("text") or "").strip():
            return None  # при exclude модель часто присылает объект с text=null
        return v

    @field_validator("unknowns", mode="before")
    @classmethod
    def _unknowns(cls, v: Any) -> list[Any]:
        return [{"text": x} if isinstance(x, str) else x for x in _as_list(v)]

    @field_validator("recommended_checks", mode="before")
    @classmethod
    def _checks(cls, v: Any) -> list[str]:
        out = []
        for x in _as_list(v):
            if isinstance(x, dict):
                x = x.get("text") or ""
            if str(x).strip():
                out.append(str(x).strip())
        return out

    @field_validator("impact_estimate", mode="before")
    @classmethod
    def _estimate(cls, v: Any) -> Any:
        if not v or (isinstance(v, dict) and not (v.get("scenario") or v.get("metric"))):
            return None
        return v

    @field_validator("bank_assessment", mode="before")
    @classmethod
    def _bank(cls, v: Any) -> Any:
        return v or {"status": "not_found"}


class ChatDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    answer: str
    evidence_ids: list[str] = Field(default_factory=list)
    insufficient: bool = False

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> list[Any]:
        return [str(x).strip() for x in _as_list(v) if str(x).strip()]


class ClassifyDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")
    topic: Literal["rates", "sanctions", "fx", "trade", "inflation", "counterparty", "industry", "other"]
    business_relevant: bool = True
    summary: str | None = None

    @field_validator("topic", mode="before")
    @classmethod
    def _topic(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v


# =========================================================================== выход


class Statement(BaseModel):
    text: str
    kind: StatementKind
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    feature_ids: list[str] = Field(default_factory=list)
    bank_chunk_ids: list[str] = Field(default_factory=list)
    interest_ids: list[str] = Field(default_factory=list)
    conditional: bool = False
    roles: list[Role] = Field(default_factory=list)


class EventRef(BaseModel):
    event_id: str
    event_version: int = 1
    reading_group_id: str | None = None
    canonical_event_id: str | None = None


class UnknownOut(BaseModel):
    text: str
    question: str | None = None
    how_to_clarify: str | None = None


class ImpactEstimateOut(BaseModel):
    metric: str
    scenario: str
    assumptions: list[str]
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    feature_ids: list[str] = Field(default_factory=list)


class BankAssessment(BaseModel):
    status: BankStatus = "not_found"
    bank_chunk_ids: list[str] = Field(default_factory=list)


class ChangesSincePrevious(BaseModel):
    """Сравнение с ранее сохранёнными выводами (память фактов, ``memory.py``).

    ``previous_insight_id`` и ``claim_ids`` ссылаются только на реально сохранённые
    записи памяти и утверждения текущего события — это проверяет QA. Без предыдущих
    данных статус ``new`` (память проверена) или ``unknown`` (память не подключена).
    """

    status: ChangeStatus = "unknown"
    previous_insight_id: str | None = None
    summary: str | None = None
    claim_ids: list[str] = Field(default_factory=list)


class Support(BaseModel):
    company_feature_ids: list[str] = Field(default_factory=list)
    event_claim_ids: list[str] = Field(default_factory=list)
    event_evidence_ids: list[str] = Field(default_factory=list)
    bank_chunk_ids: list[str] = Field(default_factory=list)
    inference_status: InferenceStatus = "hypothesis"
    limitations: list[str] = Field(default_factory=list)


class QAIssue(BaseModel):
    code: str
    severity: Literal["info", "warn", "error"] = "warn"
    message: str
    path: str | None = None


class PriorityBreakdown(BaseModel):
    """Слагаемые ``priority_score`` (для «Почему такая значимость» во фронте)."""

    materiality: float = 0.0
    urgency: float = 0.0
    evidence: float = 0.0
    cap: float | None = None
    cap_reason: str | None = None
    penalty: float = 0.0


class RankedInsight(BaseModel):
    candidate_id: str
    cluster_id: str
    event_refs: list[EventRef]
    reading_group_id: str | None = None
    canonical_event_id: str | None = None
    decision: Literal["include", "watch"]
    priority_score: float
    significance: Level
    priority_breakdown: PriorityBreakdown = Field(default_factory=PriorityBreakdown)
    impact_direction: ImpactDirection
    impact_channels: list[str] = Field(default_factory=list)
    event_status: EventStatus
    materiality: Level
    urgency: Level
    headline: str
    what_happened: str
    facts: list[Statement] = Field(default_factory=list)
    company_relevance: Statement | None = None
    impact_mechanism: Statement | None = None
    risks: list[Statement] = Field(default_factory=list)
    opportunities: list[Statement] = Field(default_factory=list)
    recommended_checks: list[str] = Field(default_factory=list)
    next_steps: list[Statement] = Field(default_factory=list)
    unknowns: list[UnknownOut] = Field(default_factory=list)
    impact_estimate: ImpactEstimateOut | None = None
    bank_assessment: BankAssessment = Field(default_factory=BankAssessment)
    ai_conclusion: Statement | None = None
    support: Support = Field(default_factory=Support)
    changes_since_previous: ChangesSincePrevious | None = None


# Статус исхода по коду причины; прочие коды — ``excluded``.
OUTCOME_STATUS = {"context_too_large": "deferred", "budget_exhausted": "deferred", "analysis_failed": "failed"}


class ExcludedCandidate(BaseModel):
    candidate_id: str
    cluster_id: str
    event_refs: list[EventRef]
    reason_code: Literal[
        "not_relevant",
        "duplicate",
        "after_as_of",
        "outside_window",
        "no_claims",
        "unreadable_source",
        "insufficient_evidence",
        "analysis_failed",
        "budget_exhausted",
        "context_too_large",
    ]
    reason: str
    headline: str | None = None
    facts: list[Statement] = Field(default_factory=list)
    reading_group_id: str | None = None
    canonical_event_id: str | None = None
    # Исход: ``excluded`` — кандидат осознанно не показывается; ``deferred`` — не обработан
    # и может быть обработан позже (бюджет, слишком большой контекст); ``failed`` — сбой анализа.
    # Не заданный явно выводится из ``reason_code``.
    status: Literal["excluded", "deferred", "failed"] | None = None
    # Пропущенные части контекста (для ``context_too_large``).
    missing_parts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_status(self) -> "ExcludedCandidate":
        if self.status is None:
            self.status = OUTCOME_STATUS.get(self.reason_code, "excluded")  # type: ignore[assignment]
        return self


class TraceEntry(BaseModel):
    step: str
    summary: str
    result: str
    status: Literal["ok", "retry", "warn"] = "ok"


class CandidateRun(BaseModel):
    """Метаданные анализа одного кандидата (идут в кэш и трассу)."""

    candidate_id: str
    model: str | None = None
    prompt_version: str
    generated_by: Literal["live", "cached", "rules"] = "live"
    cache_key: str | None = None
    created_at: str | None = None
    qa_status: Literal["passed", "passed_with_fixes", "failed", "skipped"] = "skipped"
    qa_issues: list[QAIssue] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    trace: list[TraceEntry] = Field(default_factory=list)
    # Сырой ответ модели (ID уже исходные, не псевдонимы) и шаги вызова LLM: по ним
    # QA и приоритет пересчитываются при чтении из кэша текущей версией кода.
    draft: dict[str, Any] | None = None
    llm_trace: list[TraceEntry] = Field(default_factory=list)
    # Отпечаток блока «предыдущий контекст» из памяти фактов (None — блока не было).
    memory_ref: str | None = None
    reading_group_id: str | None = None
    canonical_event_id: str | None = None


class CandidateOutcome(BaseModel):
    """Итог по кандидату: либо инсайт, либо исключение. Единица кэша."""

    insight: RankedInsight | None = None
    excluded: ExcludedCandidate | None = None
    run: CandidateRun


class RunMetadata(BaseModel):
    model: str | None = None
    prompt_version: str
    qa_status: Literal["passed", "passed_with_fixes", "failed", "skipped"] = "skipped"
    bank_rag_used: bool = False
    generated_by: Literal["live", "cached", "mixed", "rules"] = "live"
    profile_version: str | None = None
    cache_hits: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_rub: float = 0.0
    latency_ms: int = 0
    json_repairs: int = 0
    candidates: list[CandidateRun] = Field(default_factory=list)
    # Сколько кандидатов закончилось без инсайта, по кодам причин (``not_relevant``,
    # ``analysis_failed``, ``budget_exhausted``, ``context_too_large`` …).
    outcome_counts: dict[str, int] = Field(default_factory=dict)


class AnalystOutput(BaseModel):
    schema_version: Literal["AnalystOutput.v1-draft"] = ANALYST_OUTPUT_VERSION
    request_id: str
    company_id: str
    role: Role
    as_of: str
    ranked_insights: list[RankedInsight] = Field(default_factory=list)
    excluded_candidates: list[ExcludedCandidate] = Field(default_factory=list)
    run_metadata: RunMetadata
