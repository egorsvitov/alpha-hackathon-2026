"""Мост ``AnalystOutput`` → контракт фронта (``Insight``, ``FeedItem`` из ``app/schemas/base.py``).

Соответствие разделов карточки:

* «Что случилось» — ``facts`` (только утверждения источников, ``kind=fact``);
* «Почему важно вам» — ``company_relevance`` и ``impact_mechanism`` (``kind`` вывод/гипотеза);
* «Риски», «Возможности» — как есть; гипотезы помечаются ``conditional=true``;
* «Следующий шаг» — шаги для выбранной роли первыми (``roles``);
* «Оценка эффекта» — только при значимости high, сценарий с допущениями;
* «Позиция банка» — ДОСЛОВНЫЕ цитаты найденных фрагментов аналитики банка, отдельно
  от «Вывода AI»; если фрагмента нет — ``null`` (не выдумываем);
* «Что неизвестно» — неизвестное от модели, проверки, ограничения QA, противоречия;
* источники — цитаты событий, фрагменты банка и признаки клиента (``cf:<feature_id>``);
* трасса — шаги адаптера, Bank RAG, кэша, LLM, QA и правила приоритета.

``decision=exclude`` в ленте не показывается (кроме новости, загруженной пользователем);
``watch`` даёт среднюю/низкую значимость (потолок 0,45).
"""

from __future__ import annotations

from typing import Any

from app.analyst.analyst import LEVEL_RU
from app.analyst.schemas import CandidateOutcome, Candidate, CompanyContext, ImpactEstimateOut, Statement
from app.schemas.base import (
    AiConclusion,
    BankPosition,
    Claim,
    Evidence,
    FeedItem,
    ImpactEstimate,
    Insight,
    NewsEvent,
    Relevance,
    RelevanceFactor,
    TraceStep,
    UnknownItem,
    WhatHappened,
)

STATUS_RU = {
    "occurred": "произошло",
    "forecast": "прогноз",
    "scenario": "сценарий",
    "refuted": "опровергнуто",
    "mixed": "факт и прогноз",
    "unclear": "статус неясен",
}
INFERENCE_RU = {
    "supported": "подтверждено данными",
    "partially_supported": "частично подтверждено",
    "hypothesis": "гипотеза",
    "insufficient_evidence": "недостаточно оснований",
}


class EvidenceCollector:
    """Собирает ``Evidence`` в порядке первого упоминания (так нумерует фронт)."""

    def __init__(self, cand: Candidate, ctx: CompanyContext) -> None:
        self.items: list[Evidence] = []
        self._ids: set[str] = set()
        self.evidence = {}
        self.sources = {}
        for b in cand.analysis_projection.event_bundles:
            for e in b.evidence:
                self.evidence[e.evidence_id] = e
            for s in b.sources:
                self.sources[s.source_id] = s
        self.bank = {f.bank_chunk_id: f for f in cand.bank_fragments}
        self.features = {f.feature_id: f for f in ctx.verified_facts}

    def add_evidence(self, evidence_id: str) -> str | None:
        if evidence_id in self._ids:
            return evidence_id
        e = self.evidence.get(evidence_id)
        if e is None:
            return None
        src = self.sources.get(e.source_id or "")
        synthetic = bool(src.is_synthetic) if src and src.is_synthetic is not None else True
        self.items.append(
            Evidence(
                evidence_id=evidence_id,
                type="synthetic" if synthetic else "news",
                title=(src.title if src and src.title else "Источник события"),
                publisher=(src.publisher if src and src.publisher else "Источник не указан"),
                date=(src.published_at[:10] if src and src.published_at else None),
                url=src.url if src else None,
                quote=e.quote,
                is_synthetic=synthetic,
            )
        )
        self._ids.add(evidence_id)
        return evidence_id

    def add_bank(self, chunk_id: str) -> str | None:
        if chunk_id in self._ids:
            return chunk_id
        f = self.bank.get(chunk_id)
        if f is None:
            return None
        self.items.append(
            Evidence(
                evidence_id=chunk_id, type="bank_research", title=f.title, publisher=f.publisher,
                date=f.published_at, doc=f.document_id, page=f.page, quote=f.quote, is_synthetic=f.is_synthetic,
            )
        )
        self._ids.add(chunk_id)
        return chunk_id

    def add_feature(self, feature_id: str) -> str | None:
        eid = f"cf:{feature_id}"
        if eid in self._ids:
            return feature_id
        f = self.features.get(feature_id)
        if f is None:
            return None
        self.items.append(
            Evidence(
                evidence_id=eid, type="company_feature", title=f.description.split(";")[0],
                publisher="Профиль компании (синтетические данные)", date=None, doc=f.field,
                quote=f"{f.field} = {f.value}", is_synthetic=True,
            )
        )
        self._ids.add(eid)
        return feature_id

    def claim(self, s: Statement) -> Claim:
        ev_ids = [x for x in (self.add_evidence(e) for e in s.evidence_ids) if x]
        ev_ids += [x for x in (self.add_bank(b) for b in s.bank_chunk_ids) if x]
        feats = [x for x in (self.add_feature(f) for f in s.feature_ids) if x]
        return Claim(
            text=s.text,
            kind=s.kind,
            evidence_ids=ev_ids,
            features=feats or None,
            conditional=True if (s.conditional or s.kind == "hypothesis") else None,
            roles=list(s.roles) or None,
        )


def relevance_from(outcome: CandidateOutcome) -> Relevance:
    ins = outcome.insight
    if ins is None:
        reason = outcome.excluded.reason if outcome.excluded else "Анализ не выполнен"
        return Relevance(
            score=0.0, level="low", factors=[],
            explanation=f"Не влияет на компанию: {reason}",
            reasons=[reason], evidence_status="excluded",
        )
    b = ins.priority_breakdown
    parts = [
        ("analyst.materiality", f"Существенность для компании: {LEVEL_RU[ins.materiality]}", b.materiality,
         ins.company_relevance.text if ins.company_relevance else "", ins.support.company_feature_ids),
        ("analyst.urgency", f"Срочность: {LEVEL_RU[ins.urgency]}", b.urgency, f"Статус события: {STATUS_RU.get(ins.event_status, ins.event_status)}", []),
        ("analyst.evidence", f"Доказательность: {INFERENCE_RU[ins.support.inference_status]}", b.evidence,
         "Каждый вывод проверен на ссылки на утверждения источников и признаки компании", []),
    ]
    raw = sum(p[2] for p in parts)
    scale = (ins.priority_score / raw) if raw > 0 else 0.0
    factors = [
        RelevanceFactor(feature_key=k, label=label, contribution=round(v * scale, 4), detail=detail, feature_ids=list(fids))
        for k, label, v, detail, fids in parts
        if v > 0
    ]
    # Сумма вкладов должна совпадать со score (так её показывает фронт).
    if factors:
        diff = round(ins.priority_score - sum(f.contribution for f in factors), 4)
        factors[0].contribution = round(factors[0].contribution + diff, 4)
    reasons = [s.text for s in (ins.company_relevance, ins.impact_mechanism) if s]
    if b.cap_reason and b.cap is not None and ins.priority_score >= b.cap - 1e-9:
        reasons.append(f"Значимость ограничена: {b.cap_reason}")
    if b.penalty:
        reasons.append(f"Снижено проверкой QA на {b.penalty:.2f}")
    explanation = ins.company_relevance.text if ins.company_relevance else ins.headline
    return Relevance(
        score=ins.priority_score,
        level=ins.significance,
        factors=factors,
        explanation=explanation,
        reasons=reasons or None,
        candidate_channels=list(ins.impact_channels),
        missing_context=[u.text for u in ins.unknowns][:5],
        evidence_status=ins.support.inference_status,
    )


def has_bank_position(outcome: CandidateOutcome) -> bool:
    ins = outcome.insight
    return bool(ins and ins.bank_assessment.status in ("found", "outdated") and ins.bank_assessment.bank_chunk_ids)


def to_feed_item(outcome: CandidateOutcome, event: dict[str, Any], profile_version: str) -> FeedItem:
    rel = relevance_from(outcome)
    return FeedItem(
        event=NewsEvent.model_validate(event),
        relevance=rel,
        why_short=rel.explanation,
        has_bank_position=has_bank_position(outcome),
        profile_version=profile_version,
    )


def _estimate(est: ImpactEstimateOut | None, col: EvidenceCollector) -> ImpactEstimate | None:
    if est is None:
        return None
    ev = [x for x in (col.add_evidence(e) for e in est.evidence_ids) if x]
    for f in est.feature_ids:
        col.add_feature(f)
    return ImpactEstimate(metric=est.metric, summary=est.scenario, assumptions=list(est.assumptions), evidence_ids=ev)


def to_insight(
    outcome: CandidateOutcome,
    *,
    event: dict[str, Any],
    cand: Candidate,
    ctx: CompanyContext,
    company_id: str,
    role: str,
    run_id: str,
) -> Insight:
    col = EvidenceCollector(cand, ctx)
    relevance = relevance_from(outcome)
    trace = [
        TraceStep(n=i, tool=t.step, summary=t.summary, result=t.result, status=t.status)
        for i, t in enumerate(outcome.run.trace, start=1)
    ]
    generated_by = "cached" if outcome.run.generated_by == "cached" else "live"
    ins = outcome.insight

    if ins is None:
        exc = outcome.excluded
        facts = [col.claim(s) for s in (exc.facts if exc else [])]
        if not facts:
            for b in cand.analysis_projection.event_bundles:
                for c in b.claims[:1]:
                    facts.append(col.claim(Statement(text=c.text, kind="fact", claim_ids=[c.claim_id], evidence_ids=c.evidence_ids)))
        reason = exc.reason if exc else "Анализ не выполнен"
        fact_ev = [e for f in facts for e in f.evidence_ids]
        return Insight(
            run_id=run_id, company_id=company_id, profile_version=ctx.profile_version, user_role=role,  # type: ignore[arg-type]
            status="ok", generated_by=generated_by,  # type: ignore[arg-type]
            event=NewsEvent.model_validate(event), relevance=relevance,
            what_happened=WhatHappened(summary=(exc.headline if exc and exc.headline else event.get("title", "")), facts=facts),
            why_it_matters=[Claim(text=f"Аналитик не нашёл механизма влияния на бизнес компании: {reason}", kind="inference", evidence_ids=fact_ev[:2])],
            risks=[], opportunities=[], next_actions=[], impact_estimate=None, bank_position=None, ai_conclusion=None,
            unknowns=[], evidence=col.items, trace=trace,
        )

    facts = [col.claim(s) for s in ins.facts]
    why = [col.claim(s) for s in (ins.company_relevance, ins.impact_mechanism) if s]
    risks = [col.claim(s) for s in ins.risks]
    opps = [col.claim(s) for s in ins.opportunities]
    steps = [col.claim(s) for s in ins.next_steps]
    for st in steps:
        if st.kind == "fact":
            st.kind = "inference"
    ai = None
    if ins.ai_conclusion:
        c = col.claim(ins.ai_conclusion)
        ai = AiConclusion(text=c.text, evidence_ids=c.evidence_ids, features=c.features or [])
    estimate = _estimate(ins.impact_estimate, col) if ins.significance == "high" else None

    bank_position = None
    if ins.bank_assessment.status in ("found", "outdated") and ins.bank_assessment.bank_chunk_ids:
        claims, dates = [], []
        for chunk_id in ins.bank_assessment.bank_chunk_ids:
            fr = col.bank.get(chunk_id)
            if fr is None:
                continue
            col.add_bank(chunk_id)
            claims.append(Claim(text=fr.quote, kind="fact", evidence_ids=[chunk_id]))
            if fr.published_at:
                dates.append(fr.published_at)
        if claims:
            bank_position = BankPosition(as_of=max(dates) if dates else None, claims=claims)

    unknowns: list[UnknownItem] = []
    seen: set[str] = set()

    def add_unknown(text: str, question: str | None = None, how: str | None = None) -> None:
        key = text.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unknowns.append(UnknownItem(text=text, question=question, how_to_clarify=how))

    for u in ins.unknowns:
        add_unknown(u.text, u.question, u.how_to_clarify)
    for check in ins.recommended_checks:
        add_unknown(check if check.endswith("?") else f"Нужно проверить: {check}", question=check if check.endswith("?") else None)
    for lim in ins.support.limitations:
        add_unknown(lim)
    if ins.bank_assessment.status == "outdated":
        add_unknown("Позиция банка опубликована заметно раньше события и может быть неактуальной.")
    if bank_position is None:
        add_unknown("Позиции аналитики Альфа-Банка по этому событию нет: вывод сделан моделью и не является официальной позицией банка.")

    status = "insufficient_data" if (ins.support.inference_status == "insufficient_evidence" or not facts) else "ok"
    return Insight(
        run_id=run_id, company_id=company_id, profile_version=ctx.profile_version, user_role=role,  # type: ignore[arg-type]
        status=status, generated_by=generated_by,  # type: ignore[arg-type]
        event=NewsEvent.model_validate(event), relevance=relevance,
        what_happened=WhatHappened(summary=ins.headline, facts=facts),
        why_it_matters=why, risks=risks, opportunities=opps, next_actions=steps,
        impact_estimate=estimate, bank_position=bank_position, ai_conclusion=ai,
        unknowns=unknowns[:8], evidence=col.items, trace=trace,
    )
