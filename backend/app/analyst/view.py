"""Представление кандидата для модели: компактный JSON с псевдонимами ID.

Длинные ID (UUID утверждений, ``DEMO_X:/products/loan``) заменяются на ``C1``/``F1``/``B1``:
это дешевле по токенам и снижает ошибки копирования. Валидатор переводит псевдонимы
обратно; ссылки на несуществующие ID отбрасываются.
"""

from __future__ import annotations

import json
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
    RetrievalInterest,
    VerifiedFact,
)

# Лимит на цитаты первоисточника в промпте одного кандидата (оценка: байты UTF-8 / 4).
QUOTE_TOKEN_LIMIT = 1500
MAX_QUOTES_PER_CLAIM = 3
DERIVED_TEXT_NOTE = (
    "Заголовок и утверждения события — производный пересказ, не текст источника; "
    "числа и формулировки проверяй по цитатам."
)
COMPLETENESS_NOTES = {
    "unresolved": "Принадлежность кандидата к группе материалов не подтверждена: анализируется лучший переданный "
                  "документ; членство в группе не означает, что все материалы описывают одно событие.",
    "partial": "Передана только часть материалов группы.",
}
COMPLETENESS_LIMITATIONS = {
    "unresolved": "Данные неполны: кандидат не разрешён до конца, проанализирован лучший из переданных документов; "
                  "принадлежность к группе не подтверждает, что все материалы описывают одно событие.",
    "partial": "Данные неполны: передана только часть материалов группы.",
}


def _tokens(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) // 4

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
    # Цитаты первоисточника (S1…) и интересы-гипотезы (H1…): только в промптах с extras.
    quotes: dict[str, ProjectionEvidence] = field(default_factory=dict)
    interests: dict[str, RetrievalInterest] = field(default_factory=dict)
    # Пояснения для выхода (ограничения): пропущенные цитаты, неполнота кандидата.
    notes: list[str] = field(default_factory=list)
    # Данные extras, реально попавшие в промпт (для отпечатка ключа кэша); пусто — extras нет.
    extras: dict[str, Any] = field(default_factory=dict)
    _quote_by_real: dict[str, str] = field(default_factory=dict)
    _interest_by_real: dict[str, str] = field(default_factory=dict)
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

    def resolve_quote(self, ref: str) -> ProjectionEvidence | None:
        alias = self._norm(ref)
        if alias in self.quotes:
            return self.quotes[alias]
        real = self._quote_by_real.get(ref.strip())
        return self.quotes.get(real) if real else None

    def resolve_interest(self, ref: str) -> RetrievalInterest | None:
        alias = self._norm(ref)
        if alias in self.interests:
            return self.interests[alias]
        real = self._interest_by_real.get(ref.strip())
        return self.interests.get(real) if real else None

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
        q = self.resolve_quote(ref)
        if q is not None:
            return q.evidence_id
        h = self.resolve_interest(ref)
        if h is not None:
            return h.interest_id
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
                    if k in ("claim_ids", "feature_ids", "bank_chunk_ids", "quote_ids", "interest_ids") and isinstance(v, list):
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


def _select_quotes(view: PromptView, cand: Candidate) -> tuple[list[dict[str, Any]], list[str]]:
    """1–3 точных фрагмента ``evidence.quote`` на утверждение в пределах ``QUOTE_TOKEN_LIMIT``.

    Менее значимые цитаты (утверждения вне отбора ретривера и без измерений, с конца) отбрасываются
    ЦЕЛИКОМ, цитата никогда не режется посередине. Возвращает (элементы блока, id пропущенных цитат).
    """
    matched = set(cand.match_context.matched_event_claim_ids)
    order = list(view.claims.items())  # (алиас, утверждение) в исходном порядке
    ranked = sorted(
        enumerate(order),
        key=lambda t: (0 if t[1][1].claim_id in matched else 1, 0 if t[1][1].measurements else 1, t[0]),
    )
    chosen: dict[str, tuple[int, int, dict[str, Any]]] = {}
    omitted: list[str] = []
    used = 0
    for idx, (alias, claim) in ranked:
        for pos, ev_id in enumerate(claim.evidence_ids[:MAX_QUOTES_PER_CLAIM]):
            ev = view.evidence.get(ev_id)
            if ev is None or not (ev.quote or "").strip() or ev_id in chosen or ev_id in omitted:
                continue
            src = view.sources.get(ev.source_id or "")
            # Утверждение нового ретривера само является точным фрагментом оригинала (origin=original_quote):
            # текст не повторяем, помечаем совпадение — блок даёт источник, дату и координаты.
            same = getattr(claim, "origin", None) == "original_quote" and claim.text == ev.quote
            item = compact({
                "claim": alias, "source_id": ev.source_id,
                "publisher": src.publisher if src else None,
                "date": ((src.published_at or "")[:10] if src else None),
                "start": ev.start, "end": ev.end,
                "same_as_claim": True if same else None,
                "quote": None if same else ev.quote,
            })
            cost = _tokens(item) + 6  # + служебный id
            if used + cost > QUOTE_TOKEN_LIMIT:
                omitted.append(ev_id)
                continue
            used += cost
            chosen[ev_id] = (idx, pos, item)
    items = []
    for n, (ev_id, (_idx, _pos, item)) in enumerate(sorted(chosen.items(), key=lambda kv: kv[1][:2]), start=1):
        alias = f"S{n}"
        view.quotes[alias] = view.evidence[ev_id]
        view._quote_by_real[ev_id] = alias
        items.append({"id": alias, **item})
    return items, omitted


def _add_extras(view: PromptView, inp: AnalystInput, cand: Candidate, completeness: str | None) -> None:
    event = view.data.setdefault("event", {})
    if cand.derived_text:
        event["derived_text"] = True
        view.extras["derived_text"] = True
    if completeness:
        block: dict[str, Any] = {"status": completeness, "note": COMPLETENESS_NOTES[completeness]}
        if cand.group_member_refs:
            block["group_size"] = len(cand.group_member_refs)
        event["completeness"] = block
        view.extras["completeness"] = block
    items, omitted = _select_quotes(view, cand)
    if items or omitted:
        qblock: dict[str, Any] = {
            "note": "Дословные цитаты первоисточника; ссылайся на них по id (quote_ids)."
                    + (" Если same_as_claim=true, текст цитаты совпадает с текстом утверждения (claim)."
                       if any(i.get("same_as_claim") for i in items) else "")
                    + (" " + DERIVED_TEXT_NOTE if cand.derived_text else ""),
            "items": items,
        }
        if omitted:
            qblock["omitted"] = len(omitted)
            view.notes.append(
                f"Из-за лимита контекста (~{QUOTE_TOKEN_LIMIT} токенов) цитаты источника опущены целиком: {', '.join(omitted)}."
            )
        view.data["source_quotes"] = qblock
        view.extras["source_quotes"] = qblock
    interests = inp.company_context.retrieval_interests
    if interests:
        listed = []
        for i, it in enumerate(interests, start=1):
            alias = f"H{i}"
            view.interests[alias] = it
            view._interest_by_real[it.interest_id] = alias
            listed.append(compact({"id": alias, "text": it.text, "version": it.version}))
        iblock = {
            "note": "Возможные каналы влияния (гипотезы ретривера, не факты о компании и не данные профиля)",
            "items": listed,
        }
        view.data.setdefault("company", {})["possible_impact_channels"] = iblock
        view.extras["interests"] = iblock


def build_prompt_view(
    inp: AnalystInput, cand: Candidate, previous: "PreviousContext | None" = None, *, extras: bool = False
) -> PromptView:
    """``previous`` — блок памяти фактов; ``None`` — промпт без блока (как в analyst-v1.0).

    ``extras`` — блоки нового ретривера (``source_quotes``, ``possible_impact_channels``,
    ``event.completeness``/``derived_text``); включаются только при наличии данных и только для
    версий промпта, которые их описывают (``prompts.supports_extras``). Без данных — прежний вид.
    """
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

    if extras:
        # Единственный блок цитат в промпте v1.3+ — ``source_quotes`` (S1…): он же даёт алиасы для quote_ids.
        # Блок ``original_quotes`` нового ретривера дублировал бы те же фрагменты, поэтому не выводится.
        quotes_view = []
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
                "hypotheses": mc.impact_hypotheses if cand.canonical_event_id is not None else [],
            },
            "bank_fragments": bank_view,
        }
    )
    view.data["role"] = inp.request.role
    completeness = cand.completeness if cand.completeness in COMPLETENESS_LIMITATIONS else None
    if completeness:
        view.notes.append(COMPLETENESS_LIMITATIONS[completeness])
    if extras:
        _add_extras(view, inp, cand, completeness)
    if cand.canonical_event_id is not None:
        view.data["as_of"] = inp.request.as_of
        # Keep unknown dates/URLs explicit; compact() would otherwise erase them.
        view.data["event"]["sources"] = sources_view
        view.data["event"]["published_at"] = first_event.first_published_at if first_event else None
        view.data["event"]["processing_status"] = getattr(first_event, "processing_status", "unknown")
        view.data["event"]["retrieval_status"] = getattr(first_event, "retrieval_status", "unknown")
        view.data["event"]["retrieval_stop_reason"] = getattr(first_event, "retrieval_stop_reason", "unknown")
        quotes_name = "source_quotes" if extras else "original_quotes"
        view.data["event"]["evidence_policy"] = (
            f"claims и {quotes_name} — точные цитаты исходных публикаций. derived_context и заголовок merge — "
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
