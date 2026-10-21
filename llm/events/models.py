"""B1-совместимые Pydantic-модели для событий новостей.

Эти сущности соответствуют контракту B6 (docs/contracts/event-bundle.schema.json),
но сгруппированы для внутренней обработки:
  - SourceClaim — одно утверждение источника (атом);
  - CanonicalEvent — событие с привязанными утверждениями, включая конфликтующие.

Идентичность события и эквивалентность утверждений — разные решения
(см. план B1). Здесь они моделируются отдельно.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TimePeriod(BaseModel):
    """Период: {start, end} — включительные календарные границы YYYY-MM-DD.

    Одна дата задаётся равными границами; месяц — календарными границами.
    text — исходное выражение времени из статьи.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start: str | None = None
    end: str | None = None
    text: str | None = None


class ClaimTime(BaseModel):
    """Четыре независимых времени утверждения (см. B6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    occurred_at: TimePeriod | None = None
    effective_at: TimePeriod | None = None
    observation_period: TimePeriod | None = None
    forecast_horizon: TimePeriod | None = None


class Measurement(BaseModel):
    """Одно измерение: показатель, значение, единица, период сравнения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    measurement_id: str
    metric: str
    kind: str  # level | absolute_change | relative_change
    value: Any  # str (десятичное) или {"min":..,"max":..}
    unit: str
    qualifier: str = "exact"  # exact | approximate | gt | gte | lt | lte
    comparison_period: TimePeriod | None = None


class EntityRef(BaseModel):
    """Сущность (организация, человек, показатель)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entity_id: str | None = None
    name: str


class Participant(EntityRef):
    """Участник утверждения с ролью."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str = "unknown"


class SourceClaim(BaseModel):
    """Одно атомарное утверждение источника (B1).

    Уровень одного показателя и его изменения к явным базам сравнения могут
    храниться вместе (как связанные измерения). Самостоятельные действия —
    это отдельные SourceClaim.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str
    text: str = Field(min_length=1)
    modality: str = "asserted"  # asserted | forecast | plan | proposal | opinion | unknown
    polarity: str = "affirmed"  # affirmed | negated | unknown
    stage: str | None = None  # announced | proposed | approved | effective | repealed | observed
    attribution: EntityRef | None = None
    participants: list[Participant] = Field(default_factory=list)
    action: str | None = None
    object: str | None = None
    geographies: list[str] = Field(default_factory=list)
    time: ClaimTime = Field(default_factory=ClaimTime)
    measurements: list[Measurement] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    field_evidence: dict[str, list[str]] = Field(default_factory=dict)


class CanonicalEvent(BaseModel):
    """Экономическое событие/наблюдение с привязанными утверждениями.

    Происхождение и ограничения сохраняются. Спорные значения остаются
    отдельными SourceClaim, а не сливаются в одно «каноническое» число.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_version: int = 1
    title: str
    event_type: str = "other"  # policy_rate | fx_rate | price_change | ... | other
    first_published_at: str | None = None
    updated_at: str | None = None
    availability: str = "active"  # active | withdrawn
    quality: str = "complete"  # complete | partial | insufficient
    claims: list[SourceClaim] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    def to_bundle(self) -> dict[str, Any]:
        """Сериализует событие в структуру EventBundle (B6)."""
        return {
            "schema_version": "0.1",
            "event": {
                "event_id": self.event_id,
                "event_version": self.event_version,
                "title": self.title,
                "event_type": self.event_type,
                "first_published_at": self.first_published_at,
                "updated_at": self.updated_at,
                "availability": self.availability,
                "quality": self.quality,
            },
            "ranking_features": self._ranking_features(),
            "claims": [c.model_dump() for c in self.claims],
            "sources": self.sources,
            "evidence": self.evidence,
            "conflicts": self.conflicts,
            "relations": self.relations,
            "limitations": self.limitations,
        }

    def _ranking_features(self) -> dict[str, list[str]]:
        """Грубые тематические признаки для ранжировщика (B1)."""
        topics: set[str] = set()
        industries: set[str] = set()
        geographies: set[str] = set()
        objects: set[str] = set()
        metrics: set[str] = set()
        factors: set[str] = set()

        # Примитивный маппинг типа события → тема.
        TYPE_TOPIC = {
            "policy_rate": "rates",
            "fx_rate": "fx",
            "price_change": "industry",
            "regulation": "trade",
            "sanction_or_trade_restriction": "sanctions",
            "market_indicator": "market",
            "company_results": "industry",
            "investment": "industry",
        }
        topics.add(TYPE_TOPIC.get(self.event_type, "other"))
        for c in self.claims:
            geographies.update(c.geographies)
            if c.object:
                objects.add(c.object)
            for m in c.measurements:
                if m.metric:
                    metrics.add(m.metric)
        return {
            "topics": sorted(topics),
            "industries": sorted(industries),
            "geographies": sorted(geographies),
            "objects": sorted(objects),
            "metrics": sorted(metrics),
            "factors": sorted(factors),
        }