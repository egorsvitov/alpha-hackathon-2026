"""Представление кандидата для модели: компактный JSON с псевдонимами ID.

Длинные ID (UUID утверждений, ``DEMO_X:/products/loan``) заменяются на ``C1``/``F1``/``B1``:
это дешевле по токенам и снижает ошибки копирования. Валидатор переводит псевдонимы
обратно; ссылки на несуществующие ID отбрасываются.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
        }
    )


def build_prompt_view(inp: AnalystInput, cand: Candidate) -> PromptView:
    view = PromptView(data={})
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
        for s in bundle.sources:
            view.sources[s.source_id] = s
            sources_view.append(compact({"publisher": s.publisher, "date": (s.published_at or "")[:10], "synthetic": s.is_synthetic}))
        limitations.extend(bundle.limitations)
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
                "note": "Обезличенный синтетический профиль; настоящее название неизвестно и не используется",
                "facts": facts,
                "exposures": exposures,
                "related_by_payments": related,
                "unknowns": ctx.unknowns[:8],
            },
            "event": {
                "title": titles[0] if titles else None,
                "other_titles": titles[1:],
                "type": first_event.event_type if first_event else None,
                "published_at": (first_event.first_published_at or "")[:16] if first_event else None,
                "quality": first_event.quality if first_event else None,
                "claims": claims_view,
                "conflicts": conflicts_view,
                "sources": sources_view,
                "limitations": limitations,
            },
            "selection_signals": {
                "reasons": [s.reason for s in mc.signals],
                "matched_features": [view._feature_by_real[f] for f in mc.matched_company_feature_ids if f in view._feature_by_real],
                "entity_name_match": mc.confirmed_entity_match,
                "not_verified": mc.assumptions_not_verified,
            },
            "bank_fragments": bank_view,
        }
    )
    view.data["role"] = inp.request.role
    return view
