"""Представление кандидата для модели: компактный JSON с псевдонимами ID.

Длинные ID (UUID утверждений, ``DEMO_X:/products/loan``) заменяются на ``C1``/``F1``/``B1``:
это дешевле по токенам и снижает ошибки копирования. Валидатор переводит псевдонимы
обратно; ссылки на несуществующие ID отбрасываются.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from app.analyst.memory import MemoryFact, MemoryItem, PreviousContext

from app.analyst.schemas import (
    AnalystInput,
    BankFragment,
    Candidate,
    ProjectionClaim,
    ProjectionEvidence,
    ProjectionSource,
    VerifiedFact,
)

RELATION_RU = {"payment_out": "исходящие платежи компании", "payment_in": "входящие платежи компании"}
# Служебные ограничения бандлов ретривера (англ.) → кратко по-русски для модели.
LIMITATION_RU = {
    "Embedding benchmark: quote-only deterministic packaging, NOT LLM fact extraction.":
        "Утверждение — дословная цитата источника (часто анонс); структурные поля не размечены.",
    "Article identity is used as event identity; cross-article event clustering not performed.":
        "Одна статья = одно событие; статьи о том же событии не объединялись.",
    "Semantic fields unknown; extracted HTML is not certified complete original.":
        "Модальность и полярность не размечены; текст может быть неполным.",
}


def compact(value: Any) -> Any:
    """Убирает ``None``, пустые строки, списки и словари (рекурсивно)."""
    if isinstance(value, dict):
        out = {k: compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        out_l = [compact(v) for v in value]
        return [v for v in out_l if v not in (None, "", [], {})]
    return value


@dataclass
class PromptView:
    data: dict[str, Any]
    claims: dict[str, ProjectionClaim] = field(default_factory=dict)
    features: dict[str, VerifiedFact] = field(default_factory=dict)
    bank: dict[str, BankFragment] = field(default_factory=dict)
    evidence: dict[str, ProjectionEvidence] = field(default_factory=dict)
    sources: dict[str, ProjectionSource] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    # Блок «предыдущий контекст» из памяти фактов (None — блок в промпт не передавался).
    previous: "PreviousContext | None" = None
    _claim_by_real: dict[str, str] = field(default_factory=dict)
    _feature_by_real: dict[str, str] = field(default_factory=dict)
    _bank_by_real: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _norm(ref: str) -> str:
        return ref.strip().strip("[]()").upper()

    def resolve_claim(self, ref: str) -> ProjectionClaim | None:
        alias = self._norm(ref)
        if alias in self.claims:
            return self.claims[alias]
        real = self._claim_by_real.get(ref.strip())
        return self.claims.get(real) if real else None

    def resolve_feature(self, ref: str) -> VerifiedFact | None:
        alias = self._norm(ref)
        if alias in self.features:
            return self.features[alias]
        real = self._feature_by_real.get(ref.strip())
        return self.features.get(real) if real else None

    def resolve_bank(self, ref: str) -> BankFragment | None:
        alias = self._norm(ref)
        if alias in self.bank:
            return self.bank[alias]
        real = self._bank_by_real.get(ref.strip())
        return self.bank.get(real) if real else None

    def alias_of_feature(self, feature_id: str) -> str | None:
        return self._feature_by_real.get(feature_id)

    def resolve_previous_item(self, ref: str) -> "MemoryItem | None":
        """``P1`` или исходный ID записи памяти → запись (только из переданного блока)."""
        if self.previous is None or not ref:
            return None
        return self.previous.item_by_alias(ref) or self.previous.item_by_id(ref)

    def resolve_previous_claim(self, ref: str) -> "MemoryFact | None":
        """``P1.1`` или исходный ``claim_id`` сохранённого факта → факт (только из блока)."""
        if self.previous is None or not ref:
            return None
        return self.previous.fact_by_alias(ref) or self.previous.fact_by_id(ref)

    def to_real(self, ref: str) -> str:
        """Псевдоним → исходный ID (неизвестная ссылка возвращается как есть и отбросится в QA)."""
        c = self.resolve_claim(ref)
        if c is not None:
            return c.claim_id
        f = self.resolve_feature(ref)
        if f is not None:
            return f.feature_id
        b = self.resolve_bank(ref)
        if b is not None:
            return b.bank_chunk_id
        p = self.resolve_previous_claim(ref)
        if p is not None:
            return p.claim_id
        return ref

    def realias_draft(self, draft: dict[str, Any]) -> dict[str, Any]:
        """Копия ответа модели, где все псевдонимы заменены исходными ID.

        В кэше хранится именно такой черновик: при изменении адаптера (порядка признаков)
        псевдонимы сдвинутся, а исходные ID — нет; QA пересчитывается при чтении.
        """

        def fix(obj: Any) -> Any:
            if isinstance(obj, dict):
                out = {}
                for k, v in obj.items():
                    if k in ("claim_ids", "feature_ids", "bank_chunk_ids") and isinstance(v, list):
                        out[k] = [self.to_real(str(x)) for x in v]
                    elif k == "previous_insight_id" and isinstance(v, str):
                        item = self.resolve_previous_item(v)
                        out[k] = item.insight_id if item is not None else v
                    else:
                        out[k] = fix(v)
                return out
            if isinstance(obj, list):
                return [fix(x) for x in obj]
            return obj

        return fix(draft)

    def all_claims(self) -> list[ProjectionClaim]:
        return list(self.claims.values())


def _claim_view(alias: str, c: ProjectionClaim) -> dict[str, Any]:
    attribution = c.attribution.get("name") if isinstance(c.attribution, dict) else c.attribution
    measurements = [
        compact({k: m.get(k) for k in ("metric", "kind", "value", "unit", "qualifier", "comparison_period")})
        for m in c.measurements
        if isinstance(m, dict)
    ]
    return compact(
        {
            "id": alias,
            "text": c.text,
            "modality": c.modality,
            "polarity": c.polarity,
            "stage": c.stage,
            "scenario": c.scenario,
            "time": c.time,
            "measurements": measurements,
            "attribution": attribution,
            "origin": getattr(c, "origin", None),
            "evidence_ids": c.evidence_ids if getattr(c, "origin", None) == "original_quote" else None,
        }
    )


def build_prompt_view(inp: AnalystInput, cand: Candidate, previous: "PreviousContext | None" = None) -> PromptView:
    """``previous`` — блок памяти фактов; ``None`` — промпт без блока (как в analyst-v1.0)."""
    view = PromptView(data={}, previous=previous)
    ctx = inp.company_context

    facts = []
    for i, f in enumerate(ctx.verified_facts, start=1):
        alias = f"F{i}"
        view.features[alias] = f
        view._feature_by_real[f.feature_id] = alias
        facts.append(compact({"id": alias, "name": f.description, "value": f.value}))
    exposures = [
        compact(
            {
                "factor": e.factor,
                "status": e.status,
                "confidence": e.confidence,
                "basis": [view._feature_by_real[b] for b in e.basis_feature_ids if b in view._feature_by_real],
            }
        )
        for e in ctx.economic_exposures
    ]
    related = [
        compact(
            {
                "id": view._feature_by_real.get(r.entity_id) or next(
                    (view._feature_by_real[b] for b in r.basis_feature_ids if b in view._feature_by_real), None
                ),
                "name": r.name,
                "relation": RELATION_RU.get(r.relation, r.relation),
                "share_pct": r.share_pct,
                "note": r.note,
            }
        )
        for r in ctx.related_entities
    ]

    claims_view: list[dict[str, Any]] = []
    sources_view: list[dict[str, Any]] = []
    limitations: list[str] = []
    titles: list[str] = []
    conflicts_view: list[dict[str, Any]] = []
    derived_context: list[dict[str, Any]] = []
    quotes_view: list[dict[str, Any]] = []
    n = 0
    first_event = None
    for bundle in cand.analysis_projection.event_bundles:
        first_event = first_event or bundle.event
        if bundle.event.title:
            titles.append(bundle.event.title)
        for c in bundle.claims:
            n += 1
            alias = f"C{n}"
            view.claims[alias] = c
            view._claim_by_real[c.claim_id] = alias
            claims_view.append(_claim_view(alias, c))
        for e in bundle.evidence:
            view.evidence[e.evidence_id] = e
            if cand.canonical_event_id is not None:
                quotes_view.append(compact(e.model_dump(mode="json")))
        for s in bundle.sources:
            view.sources[s.source_id] = s
            if cand.canonical_event_id is not None:
                sources_view.append(s.model_dump(mode="json"))
            else:
                sources_view.append(compact({"publisher": s.publisher, "date": (s.published_at or "")[:10], "synthetic": s.is_synthetic}))
        if bundle.derived_context:
            derived_context.append(bundle.derived_context)
        limitations.extend(LIMITATION_RU.get(x, x) for x in bundle.limitations if LIMITATION_RU.get(x, x) not in limitations)
        for conflict in bundle.conflicts:
            view.conflicts.append(conflict)
            variant_aliases = [
                view._claim_by_real.get(v.get("claim_id"), v.get("claim_id"))
                for v in conflict.get("variants") or []
                if isinstance(v, dict)
            ]
            conflicts_view.append(compact({"description": conflict.get("description"), "claims": variant_aliases}))

    bank_view = []
    for i, fr in enumerate(cand.bank_fragments, start=1):
        alias = f"B{i}"
        view.bank[alias] = fr
        view._bank_by_real[fr.bank_chunk_id] = alias
        bank_view.append(
            compact({"id": alias, "date": fr.published_at, "title": fr.title, "quote": fr.quote, "applicability": fr.applicability})
        )

    mc = cand.match_context
    view.data = compact(
        {
            "role": inp.request.role,
            "as_of": inp.request.as_of[:16],
            "company": {
                "note": ("Обезличенный синтетический профиль; настоящее название неизвестно и не используется"
                         if ctx.synthetic_demo_profile else "Факты из переданного профиля; поисковые интересы не являются фактами компании"),
                "facts": facts,
                "exposures": exposures,
                "related_by_payments": related,
                "unknowns": ctx.unknowns if cand.canonical_event_id is not None else ctx.unknowns[:8],
            },
            "event": {
                "title": titles[0] if titles else None,
                "other_titles": titles[1:],
                "type": first_event.event_type if first_event else None,
                "published_at": (first_event.first_published_at or "")[:16] if first_event else None,
                "quality": first_event.quality if first_event else None,
                "claims": claims_view,
                "original_quotes": quotes_view,
                "derived_context": derived_context,
                "canonical_event_id": cand.canonical_event_id,
                "reading_group_ids": cand.reading_group_ids,
                "text_version": getattr(first_event, "text_version", None),
                "text_sha256": getattr(first_event, "text_sha256", None),
                "title_origin": getattr(first_event, "title_origin", None),
                "conflicts": conflicts_view,
                "sources": sources_view,
                "limitations": limitations,
            },
            "selection_signals": {
                "reasons": [s.reason for s in mc.signals],
                "matched_features": [view._feature_by_real[f] for f in mc.matched_company_feature_ids if f in view._feature_by_real],
                "entity_name_match": mc.confirmed_entity_match,
                "not_verified": mc.assumptions_not_verified,
                "hypotheses": mc.impact_hypotheses,
            },
            "bank_fragments": bank_view,
        }
    )
    view.data["role"] = inp.request.role
    if cand.canonical_event_id is not None:
        view.data["as_of"] = inp.request.as_of
        # Keep unknown dates/URLs explicit; compact() would otherwise erase them.
        view.data["event"]["sources"] = sources_view
        view.data["event"]["published_at"] = first_event.first_published_at if first_event else None
        view.data["event"]["processing_status"] = getattr(first_event, "processing_status", "unknown")
        view.data["event"]["retrieval_status"] = getattr(first_event, "retrieval_status", "unknown")
        view.data["event"]["retrieval_stop_reason"] = getattr(first_event, "retrieval_stop_reason", "unknown")
        view.data["event"]["evidence_policy"] = (
            "claims и original_quotes — точные цитаты исходных публикаций. derived_context и заголовок merge — "
            "производный текст, не независимые доказательства; нельзя цитировать их как отдельный источник. "
            "Ссылки подтверждают происхождение, но не доказывают смысловую корректность пересказа."
        )
    if previous is not None:
        if previous.items:
            view.data["previous_context"] = {
                "note": "Ранее сохранённые выводы аналитика по этой компании и роли о связанных событиях, "
                        "опубликованных до текущего. Это история, а не факты текущего события.",
                "items": previous.block(),
            }
        elif previous.checked:
            view.data["previous_context"] = "Связанных ранее сохранённых выводов нет."
        else:
            view.data["previous_context"] = "Память выводов не подключена: история неизвестна."
    return view
