"""LLM Business Analyst: один структурированный вызов на кандидата + QA + детерминированный приоритет.

Порядок для каждого кандидата (компания + роль + событие):

1. Детерминированные исключения до LLM: событие после ``as_of`` или вне окна, нет утверждений.
2. Кэш (память → рабочий каталог → сохранённые ответы).
3. Вызов модели через шлюз (JSON, ремонт не более 2 раз).
4. Проверка оснований ``qa.validate_draft`` (ослабление, снятие выводов, ``watch``).
5. Приоритет по явному правилу ``priority_from`` и сборка ``RankedInsight``/``ExcludedCandidate``.

Правило приоритета (шкала 0..1, пороги фронта: high ≥ 0,50; medium 0,25–0,49; low < 0,25):

* существенность по оценке модели: high 0,62 · medium 0,32 · low 0,08;
* срочность: high +0,10 · medium +0,04 · low 0;
* доказательность: supported +0,06 · partially_supported +0,03 · иначе 0;
* потолки: ``watch`` ≤ 0,45; ``hypothesis`` ≤ 0,45; ``insufficient_evidence`` ≤ 0,20;
* штраф QA: −0,05 за каждую ошибку проверки (не более −0,20).

Так «средняя» существенность никогда не становится «высокой» значимостью,
а неподтверждённая связь (``watch``) не поднимается выше средней.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from app.analyst import prompts
from app.analyst.cache import AnalysisCache
from app.analyst.input_adapter import parse_dt
from app.analyst.llm_gateway import BaseLLM, ChatRequest, LLMError, get_logger
from app.analyst.memory import MemoryStore, PreviousContext, schedule_levels
from app.analyst.qa import QAResult, clean_reason, validate_draft
from app.analyst.schemas import (
    AnalystInput,
    AnalystOutput,
    Candidate,
    CandidateOutcome,
    CandidateRun,
    EventRef,
    ExcludedCandidate,
    LLMDraft,
    PriorityBreakdown,
    RankedInsight,
    RunMetadata,
    Support,
    TraceEntry,
)
from app.analyst.view import build_prompt_view

logger = get_logger("app.analyst.analyst")
MSK = timezone(timedelta(hours=3))

MATERIALITY_BASE = {"high": 0.62, "medium": 0.32, "low": 0.08}
URGENCY_ADD = {"high": 0.10, "medium": 0.04, "low": 0.0}
EVIDENCE_ADD = {"supported": 0.06, "partially_supported": 0.03, "hypothesis": 0.0, "insufficient_evidence": 0.0}
LEVEL_RU = {"high": "высокая", "medium": "средняя", "low": "низкая"}


class AnalysisUnavailable(Exception):
    """Нет ни кэша, ни возможности живого вызова (нет ключа, бюджет, сбой шлюза)."""

    def __init__(self, reason: str, code: str = "unavailable") -> None:
        super().__init__(reason)
        self.code = code


def level_from_score(score: float) -> str:
    return "high" if score >= 0.5 else ("medium" if score >= 0.25 else "low")


def priority_from(qa: QAResult, materiality: str, urgency: str) -> tuple[float, str, PriorityBreakdown]:
    m = MATERIALITY_BASE.get(materiality, 0.08)
    u = URGENCY_ADD.get(urgency, 0.0)
    e = EVIDENCE_ADD.get(qa.inference_status, 0.0)
    raw = m + u + e
    cap, cap_reason = None, None
    if qa.decision == "watch":
        cap, cap_reason = 0.45, "решение watch: связь не подтверждена"
    if qa.inference_status == "hypothesis" and (cap is None or cap > 0.45):
        cap, cap_reason = 0.45, "вывод — гипотеза"
    if qa.inference_status == "insufficient_evidence":
        cap, cap_reason = 0.20, "недостаточно оснований"
    score = raw if cap is None else min(raw, cap)
    score = max(0.0, score - qa.penalty)
    score = round(min(score, 0.95), 2)
    return score, level_from_score(score), PriorityBreakdown(
        materiality=m, urgency=u, evidence=e, cap=cap, cap_reason=cap_reason, penalty=qa.penalty
    )


def cache_key(
    company_id: str, cand: Candidate, role: str, profile_version: str, prompt_version: str, model: str, memory_ref: str | None = None
) -> str:
    """Ключ кэша. ``memory_ref`` — отпечаток блока памяти (версия схемы памяти + содержимое
    записей, отобранных по дате); без блока ключ совпадает с прежним, сохранённые ответы валидны."""
    ref = cand.event_bundle_refs[0]
    parts = [company_id, f"{ref.event_id}@{ref.event_version}#{ref.snapshot_sha256[:12]}", role, profile_version, prompt_version, model]
    if memory_ref:
        parts.append(memory_ref)
    return "|".join(parts)


def loose_key(company_id: str, cand: Candidate, role: str, profile_version: str, prompt_version: str) -> str:
    ref = cand.event_bundle_refs[0]
    return "|".join([company_id, f"{ref.event_id}@{ref.event_version}#{ref.snapshot_sha256[:12]}", role, profile_version, prompt_version])


def _event_refs(cand: Candidate) -> list[EventRef]:
    return [
        EventRef(
            event_id=r.event_id, event_version=r.event_version,
            reading_group_id=cand.reading_group_id, canonical_event_id=cand.canonical_event_id,
        )
        for r in cand.event_bundle_refs
    ]


def _identity(cand: Candidate) -> dict[str, str | None]:
    return {"reading_group_id": cand.reading_group_id, "canonical_event_id": cand.canonical_event_id}


# Коды исходов без инсайта, которые НЕ означают «событие не важно»: сбой, бюджет, слишком большой контекст.
NON_VERDICT_CODES = frozenset({"analysis_failed", "budget_exhausted", "context_too_large"})


class BusinessAnalyst:
    def __init__(
        self,
        llm: BaseLLM | None,
        *,
        model: str,
        cache: AnalysisCache | None = None,
        max_tokens: int = 1600,
        max_parallel: int = 4,
        fallback_models: list[str] | None = None,
        prompt_version: str = prompts.PROMPT_VERSION,
        memory: MemoryStore | None = None,
        fewshot: bool = False,
    ) -> None:
        if prompt_version not in prompts.SYSTEM_PROMPTS:
            raise ValueError(f"Неизвестная версия промпта: {prompt_version}")
        self.llm = llm
        self.model = model
        self.cache = cache
        self.max_tokens = max_tokens
        self.max_parallel = max(1, max_parallel)
        self.fallback_models = fallback_models or []
        self.fewshot = fewshot
        # Версия для ключа кэша и метаданных: с примерами — отдельные записи кэша.
        self.prompt_version = prompt_version + (prompts.FEWSHOT_SUFFIX if fewshot else "")
        self.base_prompt_version = prompt_version
        self.memory = memory
        self.qa_totals = {"statements": 0, "with_refs": 0, "candidates": 0, "qa_failed": 0, "bank_found": 0}

    # ------------------------------------------------------------------ память фактов
    def previous_context(self, inp: AnalystInput, cand: Candidate) -> PreviousContext | None:
        """Блок памяти для кандидата. ``None`` — версия промпта не работает с памятью."""
        if not prompts.supports_memory(self.base_prompt_version):
            return None
        if self.memory is None:
            return PreviousContext(items=[], checked=False)
        return self.memory.context_for(inp.request.company_id, inp.request.role, cand, parse_dt(inp.request.as_of))

    def _remember(self, inp: AnalystInput, cand: Candidate, outcome: CandidateOutcome) -> None:
        if self.memory is None:
            return
        try:
            self.memory.record_outcome(inp.request.company_id, inp.request.role, cand, outcome)
        except Exception as exc:  # noqa: BLE001 - память не должна ронять анализ
            logger.warning("Не удалось сохранить вывод в память: %s", type(exc).__name__)

    # ------------------------------------------------------------------ один кандидат
    def precheck(self, inp: AnalystInput, cand: Candidate) -> ExcludedCandidate | None:
        excluded = self._precheck(inp, cand)
        if excluded is not None:
            for k, v in _identity(cand).items():
                setattr(excluded, k, v)
        return excluded

    def _precheck(self, inp: AnalystInput, cand: Candidate) -> ExcludedCandidate | None:
        as_of = parse_dt(inp.request.as_of)
        bundle = cand.analysis_projection.event_bundles[0] if cand.analysis_projection.event_bundles else None
        if bundle is None or not bundle.claims:
            return ExcludedCandidate(
                candidate_id=cand.candidate_id, cluster_id=cand.cluster_id, event_refs=_event_refs(cand),
                reason_code="no_claims", reason="В событии нет утверждений источников — анализ невозможен",
                headline=bundle.event.title if bundle else None,
            )
        if "unreadable_claims" in cand.quality_flags:
            return ExcludedCandidate(
                candidate_id=cand.candidate_id, cluster_id=cand.cluster_id, event_refs=_event_refs(cand),
                reason_code="unreadable_source",
                reason="Текст источника повреждён (ошибка кодировки): утверждения нечитаемы, по одному заголовку выводы не делаем",
                headline=bundle.event.title,
            )
        published = parse_dt(bundle.event.first_published_at)
        if as_of and published and published > as_of:
            return ExcludedCandidate(
                candidate_id=cand.candidate_id, cluster_id=cand.cluster_id, event_refs=_event_refs(cand),
                reason_code="after_as_of", reason=f"Событие опубликовано после момента анализа ({inp.request.as_of[:16]})",
                headline=bundle.event.title,
            )
        if as_of and published and inp.request.news_window_hours > 0:
            if (as_of - published).total_seconds() > inp.request.news_window_hours * 3600:
                return ExcludedCandidate(
                    candidate_id=cand.candidate_id, cluster_id=cand.cluster_id, event_refs=_event_refs(cand),
                    reason_code="outside_window", reason=f"Событие старше окна новостей ({inp.request.news_window_hours} ч)",
                    headline=bundle.event.title,
                )
        if "context_too_large" in cand.quality_flags:
            # Контекст не помещается в бюджет даже после усечения: модели не отправляем и не режем молча.
            missing = list(cand.omitted_parts) or ["контекст события превышает бюджет промпта"]
            return ExcludedCandidate(
                candidate_id=cand.candidate_id, cluster_id=cand.cluster_id, event_refs=_event_refs(cand),
                reason_code="context_too_large",
                reason="Контекст кандидата не помещается в бюджет промпта даже после усечения — отложен, без вызова модели",
                headline=bundle.event.title, status="deferred", missing_parts=missing,
            )
        return None

    def analyze_candidate(
        self,
        inp: AnalystInput,
        cand: Candidate,
        *,
        allow_live: bool = True,
        prefer_cache: bool = True,
        previous: PreviousContext | None = None,
    ) -> CandidateOutcome:
        if previous is None:
            previous = self.previous_context(inp, cand)
        outcome = self._analyze_candidate(inp, cand, allow_live=allow_live, prefer_cache=prefer_cache, previous=previous)
        if outcome.run.generated_by != "rules":
            self._remember(inp, cand, outcome)
        return outcome

    def _analyze_candidate(
        self,
        inp: AnalystInput,
        cand: Candidate,
        *,
        allow_live: bool,
        prefer_cache: bool,
        previous: PreviousContext | None,
    ) -> CandidateOutcome:
        company_id = inp.request.company_id
        role = inp.request.role
        pv = inp.company_context.profile_version
        pver = self.prompt_version
        mem_ref = previous.ref() if previous is not None else None
        now = datetime.now(MSK).isoformat(timespec="seconds")
        base_trace = [
            TraceEntry(
                step="input_adapter",
                summary="Сборка AnalystInput.v1-draft (профиль, утверждения события, сигналы отбора)",
                result=(
                    f"Признаков компании: {len(inp.company_context.verified_facts)}, утверждений события: "
                    f"{sum(len(b.claims) for b in cand.analysis_projection.event_bundles)}, "
                    f"противоречий: {sum(len(b.conflicts) for b in cand.analysis_projection.event_bundles)}"
                ),
            ),
            TraceEntry(
                step="bank_rag_search",
                summary="Поиск фрагментов аналитики банка, опубликованных до момента анализа",
                result=(
                    f"Найдено фрагментов: {len(cand.bank_fragments)}"
                    if cand.bank_fragments
                    else "Подходящих материалов банка не найдено — позицию банка не формируем"
                ),
            ),
        ]
        if previous is not None:
            base_trace.append(
                TraceEntry(
                    step="memory_lookup",
                    summary="Поиск ранее сохранённых выводов по компании и роли (только события до текущего и до as_of)",
                    result=(
                        f"Найдено связанных записей: {len(previous.items)} ({', '.join(it.insight_id for it in previous.items)})"
                        if previous.items
                        else ("Связанных записей нет" if previous.checked else "Память выводов не подключена")
                    ),
                )
            )

        excluded = self.precheck(inp, cand)
        if excluded is not None:
            run = CandidateRun(
                candidate_id=cand.candidate_id, model=None, prompt_version=pver, generated_by="rules",
                created_at=now, qa_status="skipped",
                trace=base_trace + [TraceEntry(step="precheck", summary="Проверка времени и полноты события", result=excluded.reason, status="warn")],
            )
            return CandidateOutcome(excluded=excluded, run=run)

        key = cache_key(company_id, cand, role, pv, pver, self.model, mem_ref)
        lkey = loose_key(company_id, cand, role, pv, pver)
        label = f"{company_id}/{cand.event_bundle_refs[0].event_id}__{role}"

        lock = self.cache.key_lock(key) if self.cache else None
        if lock:
            lock.acquire()
        try:
            if self.cache:
                for m in [self.model, *self.fallback_models]:
                    record, source = self.cache.get(cache_key(company_id, cand, role, pv, pver, m, mem_ref))
                    if record is not None and (prefer_cache or source in ("memory", "runtime") or not allow_live):
                        return self._from_cache(record, source or "memory", inp, cand, base_trace, previous,
                                                allow_live=allow_live and self.llm is not None)
            if not allow_live or self.llm is None:
                return self._offline(lkey, inp, cand, base_trace, previous, reason="живой вызов LLM недоступен в этом режиме")
            outcome, used_model, last_exc = None, self.model, None
            for model in [self.model, *self.fallback_models]:
                try:
                    outcome = self._live(inp, cand, base_trace, now, model, previous)
                    used_model = model
                    break
                except AnalysisUnavailable as exc:
                    last_exc = exc
                    if exc.code in ("budget_exhausted", "no_key", "auth"):
                        break
                    logger.warning("Модель %s не справилась (%s), пробую запасную", model, exc.code)
            if outcome is None:
                assert last_exc is not None
                return self._offline(lkey, inp, cand, base_trace, previous, reason=str(last_exc), code=last_exc.code)
            used_key = cache_key(company_id, cand, role, pv, pver, used_model, mem_ref)
            outcome.run.cache_key = used_key
            outcome.run.memory_ref = mem_ref
            if self.cache:
                self.cache.put(
                    used_key, outcome.model_dump(mode="json"), loose_key=lkey, label=label, model=used_model, created_at=now
                )
            return outcome
        finally:
            if lock:
                lock.release()

    def _restore(
        self,
        record: dict[str, Any],
        inp: AnalystInput,
        cand: Candidate,
        base_trace: list[TraceEntry],
        previous: PreviousContext | None = None,
    ) -> CandidateOutcome:
        """Восстановление из кэша: QA и приоритет пересчитываются по сохранённому ответу модели.

        Сравнение с прошлым проверяется по ТЕКУЩЕМУ блоку памяти: при офлайн-откате на
        ответ с другим контекстом ссылки на отсутствующие записи снимаются (``unknown``).
        """
        stored = CandidateOutcome.model_validate(record)
        if stored.run.draft is None:
            return stored
        try:
            draft = LLMDraft.model_validate(stored.run.draft)
        except Exception:  # noqa: BLE001 - старая схема черновика: отдаём как сохранено
            return stored
        view = build_prompt_view(inp, cand, previous=previous)
        qa = validate_draft(draft, view, inp.request.role, as_of=inp.request.as_of)
        outcome = self._build(inp, cand, draft, qa, list(base_trace) + list(stored.run.llm_trace))
        outcome.run.model = stored.run.model
        outcome.run.prompt_version = stored.run.prompt_version
        outcome.run.created_at = stored.run.created_at
        outcome.run.usage = stored.run.usage
        outcome.run.cache_key = stored.run.cache_key
        outcome.run.generated_by = stored.run.generated_by
        outcome.run.draft = stored.run.draft
        outcome.run.llm_trace = stored.run.llm_trace
        outcome.run.memory_ref = stored.run.memory_ref
        return outcome

    def _from_cache(
        self,
        record: dict[str, Any],
        source: str,
        inp: AnalystInput,
        cand: Candidate,
        base_trace: list[TraceEntry],
        previous: PreviousContext | None = None,
        allow_live: bool = True,
    ) -> CandidateOutcome:
        outcome = self._restore(record, inp, cand, base_trace, previous)
        # Сохранённый ответ репозитория или режим без живых вызовов (cached) — «cached»;
        # в режиме llm ответ, только что посчитанный живым вызовом и взятый из памяти, остаётся «live».
        if source == "bundled" or not allow_live:
            outcome.run.generated_by = "cached"
        outcome.run.trace = list(outcome.run.trace) + [
            TraceEntry(
                step="cache_lookup",
                summary="Повторный запрос: результат взят из кэша, бюджет не расходуется",
                result={"bundled": "Сохранённый ответ из репозитория", "runtime": "Рабочий кэш на диске", "memory": "Кэш в памяти"}.get(source, source),
            )
        ]
        return outcome

    def _offline(
        self,
        lkey: str,
        inp: AnalystInput,
        cand: Candidate,
        base_trace: list[TraceEntry],
        previous: PreviousContext | None = None,
        *,
        reason: str,
        code: str = "unavailable",
    ) -> CandidateOutcome:
        if self.cache:
            record, source = self.cache.get_loose(lkey, prefer_models=[self.model, *self.fallback_models])
            if record is None and self.prompt_version != prompts.PROMPT_VERSION:
                fallback_lkey = loose_key(
                    inp.request.company_id,
                    cand,
                    inp.request.role,
                    inp.company_context.profile_version,
                    prompts.PROMPT_VERSION,
                )
                record, source = self.cache.get_loose(fallback_lkey, prefer_models=[self.model, *self.fallback_models])
            if record is not None:
                outcome = self._restore(record, inp, cand, base_trace, previous)
                outcome.run.generated_by = "cached"
                outcome.run.trace = list(outcome.run.trace) + [
                    TraceEntry(
                        step="cache_lookup",
                        summary="Живой вызов невозможен — показан сохранённый ответ",
                        result=f"Причина: {reason}. Источник: {source}, модель {outcome.run.model}",
                        status="warn",
                    )
                ]
                return outcome
        raise AnalysisUnavailable(reason, code)

    def _live(
        self,
        inp: AnalystInput,
        cand: Candidate,
        base_trace: list[TraceEntry],
        now: str,
        model: str,
        previous: PreviousContext | None = None,
    ) -> CandidateOutcome:
        assert self.llm is not None
        view = build_prompt_view(inp, cand, previous=previous)
        messages = prompts.build_analyst_messages(view.data, version=self.base_prompt_version, fewshot=self.fewshot)
        request = ChatRequest(messages=messages, model=model, max_tokens=self.max_tokens, temperature=0.1, purpose="analysis")
        try:
            draft, usage = self.llm.chat_json(request, LLMDraft.model_validate)
        except LLMError as exc:
            logger.warning("Анализ %s не выполнен: %s", cand.candidate_id, exc)
            raise AnalysisUnavailable(str(exc), getattr(exc, "code", "llm_error")) from None

        llm_trace: list[TraceEntry] = []
        for i, err in enumerate(usage.repair_errors[: usage.json_repairs]):
            llm_trace.append(TraceEntry(step="llm_analyst", summary=f"Ответ модели не прошёл схему (попытка {i + 1})", result=f"Повтор с ремонтным промптом: {err[:160]}", status="retry"))
        llm_trace.append(
            TraceEntry(
                step="llm_analyst",
                summary=f"Анализ кандидата моделью {model} (промпт {self.prompt_version})",
                result=(
                    f"Решение модели: {draft.decision}, существенность {draft.materiality}; токены {usage.prompt_tokens}/"
                    f"{usage.completion_tokens}, {usage.cost_rub:.4f} ₽, {usage.latency_ms / 1000:.1f} с"
                ),
            )
        )
        draft = LLMDraft.model_validate(view.realias_draft(draft.model_dump(mode="json")))
        qa = validate_draft(draft, view, inp.request.role, as_of=inp.request.as_of)
        outcome = self._build(inp, cand, draft, qa, list(base_trace) + llm_trace)
        outcome.run.model = model
        outcome.run.created_at = now
        outcome.run.usage = usage.as_dict()
        outcome.run.draft = draft.model_dump(mode="json")
        outcome.run.llm_trace = llm_trace
        return outcome

    # ------------------------------------------------------------------ сборка результата
    def _build(self, inp: AnalystInput, cand: Candidate, draft: LLMDraft, qa: QAResult, trace: list[TraceEntry]) -> CandidateOutcome:
        n_err = sum(1 for i in qa.issues if i.severity == "error")
        n_warn = sum(1 for i in qa.issues if i.severity == "warn")
        codes = sorted({i.code for i in qa.issues if i.severity != "info"})
        trace.append(
            TraceEntry(
                step="qa_validator",
                summary="Проверка ссылок, чисел, модальности и запрещённых формулировок",
                result=f"Итог: {qa.status}; ошибок {n_err}, замечаний {n_warn}" + (f" ({', '.join(codes)})" if codes else ""),
                status="ok" if qa.status == "passed" else "warn",
            )
        )
        run = CandidateRun(
            candidate_id=cand.candidate_id, prompt_version=self.prompt_version, generated_by="live",
            qa_status=qa.status, qa_issues=qa.issues, trace=trace,  # type: ignore[arg-type]
            **_identity(cand),
        )
        if qa.changes is not None:
            ch = qa.changes
            trace.append(TraceEntry(
                step="memory_compare", summary="Сравнение с ранее сохранёнными выводами (проверено QA)",
                result=f"{ch.status}" + (f" → {ch.previous_insight_id}" if ch.previous_insight_id else ""),
            ))
        if qa.decision == "exclude":
            trace.append(TraceEntry(step="priority_rule", summary="Решение аналитика", result="exclude: событие не показывается в ленте этой роли"))
            run.trace = list(trace)  # Pydantic копирует список при создании run — переносим шаги, добавленные позже
            return CandidateOutcome(
                excluded=ExcludedCandidate(
                    candidate_id=cand.candidate_id, cluster_id=cand.cluster_id, event_refs=_event_refs(cand),
                    reason_code="not_relevant",
                    reason=(clean_reason(draft.exclude_reason or "") or "Аналитик не нашёл механизма влияния на компанию")[:400],
                    headline=qa.headline, facts=qa.facts, **_identity(cand),
                ),
                run=run,
            )

        score, level, breakdown = priority_from(qa, draft.materiality, draft.urgency)
        if qa.impact_estimate is not None and level != "high":
            qa.impact_estimate = None
            trace.append(TraceEntry(step="qa_validator", summary="Оценка эффекта", result="Снята: показывается только при высокой значимости", status="warn"))
        trace.append(
            TraceEntry(
                step="priority_rule",
                summary="Детерминированная оценка приоритета (пороги 0,50 / 0,25)",
                result=(
                    f"{qa.decision}: {score:.2f} → {level}"
                    + (f"; ограничение: {breakdown.cap_reason}" if breakdown.cap_reason and breakdown.cap is not None and score >= breakdown.cap - 1e-9 else "")
                    + (f"; штраф QA −{breakdown.penalty:.2f}" if breakdown.penalty else "")
                ),
            )
        )
        feats, claims, evs, banks = qa.support_ids()
        insight = RankedInsight(
            candidate_id=cand.candidate_id, cluster_id=cand.cluster_id, event_refs=_event_refs(cand), **_identity(cand),
            decision=qa.decision,  # type: ignore[arg-type]
            priority_score=score, significance=level,  # type: ignore[arg-type]
            priority_breakdown=breakdown,
            impact_direction=draft.impact_direction, impact_channels=list(draft.impact_channels),
            event_status=qa.event_status,  # type: ignore[arg-type]
            materiality=draft.materiality, urgency=draft.urgency,
            headline=qa.headline,
            what_happened=" ".join(s.text for s in qa.facts[:2]),
            facts=qa.facts,
            company_relevance=qa.company_relevance,
            impact_mechanism=qa.impact_mechanism,
            risks=qa.risks, opportunities=qa.opportunities,
            recommended_checks=qa.recommended_checks,
            next_steps=qa.next_steps,
            unknowns=qa.unknowns,
            impact_estimate=qa.impact_estimate,
            bank_assessment=qa.bank_assessment,
            ai_conclusion=qa.ai_conclusion,
            changes_since_previous=qa.changes,
            support=Support(
                company_feature_ids=feats, event_claim_ids=claims, event_evidence_ids=evs, bank_chunk_ids=banks,
                inference_status=qa.inference_status,  # type: ignore[arg-type]
                limitations=qa.limitations,
            ),
        )
        run.trace = list(trace)
        return CandidateOutcome(insight=insight, run=run)

    # ------------------------------------------------------------------ пакет
    def analyze(self, inp: AnalystInput, *, allow_live: bool = True, prefer_cache: bool = True) -> tuple[AnalystOutput, dict[str, CandidateOutcome | AnalysisUnavailable]]:
        """Анализ всех кандидатов (параллельно) + дедупликация + сортировка.

        Возвращает ``AnalystOutput`` и словарь ``candidate_id → outcome | AnalysisUnavailable``
        (для отката отдельных кандидатов на шаблон).
        """
        results: dict[str, CandidateOutcome | AnalysisUnavailable] = {}

        def work(c: Candidate) -> tuple[str, CandidateOutcome | AnalysisUnavailable]:
            try:
                return c.candidate_id, self.analyze_candidate(inp, c, allow_live=allow_live, prefer_cache=prefer_cache)
            except AnalysisUnavailable as exc:
                return c.candidate_id, exc

        # С памятью фактов: связанные кандидаты — по возрастанию даты (уровни), несвязанные —
        # параллельно. Тогда результат не зависит от порядка кандидатов во входе.
        levels = schedule_levels(list(inp.candidates)) if self.memory is not None else [list(inp.candidates)]
        for level in levels:
            if len(level) <= 1 or self.max_parallel == 1:
                pairs = [work(c) for c in level]
            else:
                with ThreadPoolExecutor(max_workers=min(self.max_parallel, len(level))) as pool:
                    pairs = list(pool.map(work, level))
            results.update(pairs)
        return self.assemble(inp, results), results

    def assemble(self, inp: AnalystInput, results: dict[str, CandidateOutcome | AnalysisUnavailable]) -> AnalystOutput:
        ranked: list[RankedInsight] = []
        excluded: list[ExcludedCandidate] = []
        runs: list[CandidateRun] = []
        failed_ids: set[str] = set()
        for cand in inp.candidates:
            res = results.get(cand.candidate_id)
            if isinstance(res, AnalysisUnavailable) or res is None:
                # Сбой API, таймаут, исчерпанный бюджет — это НЕ вердикт «не важно» (not_relevant).
                code = "budget_exhausted" if isinstance(res, AnalysisUnavailable) and res.code == "budget_exhausted" else "analysis_failed"
                reason = f"Анализ не выполнен: {res}" if res else "Анализ не выполнен"
                if code == "budget_exhausted":
                    reason = f"Бюджет LLM исчерпан, кандидат не проанализирован: {res}"
                excluded.append(
                    ExcludedCandidate(
                        candidate_id=cand.candidate_id, cluster_id=cand.cluster_id, event_refs=_event_refs(cand),
                        reason_code=code, reason=reason, **_identity(cand),  # type: ignore[arg-type]
                    )
                )
                # Кандидат попадает в трассу и метрики прогона, но не считается «сгенерированным правилами».
                runs.append(CandidateRun(
                    candidate_id=cand.candidate_id, model=None, prompt_version=self.prompt_version, generated_by="rules",
                    qa_status="skipped", **_identity(cand),
                    trace=[TraceEntry(step="llm_analyst", summary="Анализ кандидата не выполнен", result=reason[:300], status="warn")],
                ))
                failed_ids.add(cand.candidate_id)
                continue
            runs.append(res.run)
            if res.insight:
                ranked.append(res.insight)
            elif res.excluded:
                excluded.append(res.excluded)
        ranked.sort(key=lambda r: (-r.priority_score, r.candidate_id))
        # Защита от дублей. Если задан canonical_event_id — дубль только при его совпадении
        # (общий кластер/группа чтения и пересекающиеся источники дублем не считаются).
        # Без canonical_event_id — прежнее правило: один кластер или один event_id — одно уведомление.
        seen_canonical: set[str] = set()
        seen_clusters: set[str] = set()
        seen_titles: set[str] = set()
        unique: list[RankedInsight] = []
        for r in ranked:
            canonical = r.canonical_event_id
            if canonical:
                is_dup = canonical in seen_canonical
            else:
                title_key = r.event_refs[0].event_id if r.event_refs else r.candidate_id
                is_dup = r.cluster_id in seen_clusters or title_key in seen_titles
            if is_dup:
                excluded.append(
                    ExcludedCandidate(
                        candidate_id=r.candidate_id, cluster_id=r.cluster_id, event_refs=r.event_refs,
                        reason_code="duplicate", reason="Дубликат события, уже показанного выше", headline=r.headline,
                        reading_group_id=r.reading_group_id, canonical_event_id=canonical,
                    )
                )
                continue
            if canonical:
                seen_canonical.add(canonical)
            else:
                seen_clusters.add(r.cluster_id)
                seen_titles.add(r.event_refs[0].event_id if r.event_refs else r.candidate_id)
            unique.append(r)
        if len(unique) > inp.request.max_insights:
            for r in unique[inp.request.max_insights :]:
                excluded.append(
                    ExcludedCandidate(
                        candidate_id=r.candidate_id, cluster_id=r.cluster_id, event_refs=r.event_refs,
                        reason_code="insufficient_evidence", reason="За пределами лимита инсайтов запроса", headline=r.headline,
                        reading_group_id=r.reading_group_id, canonical_event_id=r.canonical_event_id,
                    )
                )
            unique = unique[: inp.request.max_insights]

        statuses = {r.qa_status for r in runs if r.qa_status != "skipped"}
        qa_status = "failed" if "failed" in statuses else ("passed_with_fixes" if "passed_with_fixes" in statuses else ("passed" if statuses else "skipped"))
        gens = {r.generated_by for r in runs if r.candidate_id not in failed_ids}
        generated = gens.pop() if len(gens) == 1 else ("mixed" if gens else "rules")
        usage = [r.usage for r in runs if r.usage]
        meta = RunMetadata(
            model=self.model, prompt_version=self.prompt_version, qa_status=qa_status,  # type: ignore[arg-type]
            bank_rag_used=any(c.bank_fragments for c in inp.candidates),
            generated_by=generated,  # type: ignore[arg-type]
            profile_version=inp.company_context.profile_version,
            cache_hits=sum(1 for r in runs if any(t.step == "cache_lookup" for t in r.trace)),
            llm_calls=sum(int(u.get("calls", 0)) for u in usage),
            prompt_tokens=sum(int(u.get("prompt_tokens", 0)) for u in usage),
            completion_tokens=sum(int(u.get("completion_tokens", 0)) for u in usage),
            reasoning_tokens=sum(int(u.get("reasoning_tokens", 0)) for u in usage),
            cost_rub=round(sum(float(u.get("cost_rub", 0.0)) for u in usage), 6),
            latency_ms=sum(int(u.get("latency_ms", 0)) for u in usage),
            json_repairs=sum(int(u.get("json_repairs", 0)) for u in usage),
            candidates=runs,
            outcome_counts=dict(sorted(Counter(e.reason_code for e in excluded).items())),
        )
        return AnalystOutput(
            request_id=inp.request.request_id, company_id=inp.request.company_id, role=inp.request.role, as_of=inp.request.as_of,
            ranked_insights=unique, excluded_candidates=excluded, run_metadata=meta,
        )

