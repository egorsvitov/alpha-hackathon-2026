"""Фасад аналитика для бэкенда (лента, карточка, чат, загрузка новости, метрики).

Режимы (``ANALYST_MODE``):

* ``template`` — фасад не используется, работает прежний шаблонный путь ``store``/``orchestrator``;
* ``llm`` — кэш → живой вызов через шлюз → сохранённый ответ (``generated_by=cached``) →
  шаблон (с пометкой в трассе), если ни кэша, ни возможности вызова нет;
* ``cached`` — только сохранённые ответы, сеть не используется; промах → шаблон.

Отсутствие ключа или исчерпание бюджета не роняет сервис.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.analyst import prompts
from app.analyst.analyst import AnalysisUnavailable, BusinessAnalyst
from app.analyst.bridge import relevance_from, to_feed_item, to_insight
from app.analyst.cache import AnalysisCache, key_hash
from app.analyst.input_adapter import bank_fragments_from_evidence, build_analyst_input, parse_dt
from app.analyst.llm_gateway import (
    BaseLLM,
    BudgetLedger,
    ChatRequest,
    GatewayStats,
    LLMError,
    LLMGateway,
    get_logger,
)
from app.analyst.memory import MemoryStore
from app.analyst.qa import numbers_in
from app.analyst.schemas import AnalystOutput, CandidateOutcome, ChatDraft, ClassifyDraft
from app.analyst.settings import AnalystSettings
from app.schemas.base import Evidence, FeedItem, Insight, QaAnswer, TraceStep

logger = get_logger("app.analyst.service")
MSK = timezone(timedelta(hours=3))

TOPIC_LABELS = {
    "rates": "Ставки и кредит",
    "sanctions": "Ограничения и торговля",
    "fx": "Валюта",
    "trade": "Пошлины и экспорт",
    "inflation": "Инфляция и спрос",
    "counterparty": "Контрагенты",
    "industry": "Отраслевые события",
    "other": "Пользовательское",
}
TOPIC_KEYWORDS = [
    ("rates", r"ставк|кредит|депозит|ипотек|ДКП|Банк России|ЦБ"),
    ("fx", r"рубл|курс|валют|юан|доллар|евро"),
    ("sanctions", r"санкц|ограничен|квот|эмбарго|запрет"),
    ("trade", r"пошлин|экспорт|импорт|таможн|уголь|нефт|газ|сырь"),
    ("inflation", r"инфляц|цены|подорож|зарплат|спрос"),
]

ScoreFn = Callable[[dict[str, Any], dict[str, Any], str], Any]


class AnalystService:
    def __init__(
        self,
        settings: AnalystSettings | None = None,
        *,
        llm: BaseLLM | None = None,
        cache: AnalysisCache | None = None,
        ledger: BudgetLedger | None = None,
        bank_evidence: list[Any] | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self.settings = settings or AnalystSettings.from_env()
        s = self.settings
        self.stats = llm.stats if llm is not None else GatewayStats()
        if ledger is None:
            ledger = llm.ledger if (llm is not None and llm.ledger is not None) else BudgetLedger(
                s.daily_budget_rub, session_cap_rub=s.session_cap_rub, state_path=s.runtime_cache_dir / "budget_ledger.json"
            )
        self.ledger = ledger
        if llm is None and s.mode == "llm" and s.has_key:
            llm = LLMGateway(
                api_key=s.api_key, base_url=s.base_url, fallback_base_url=s.fallback_base_url, ledger=ledger,
                stats=self.stats, timeout_s=s.timeout_s, max_retries=s.max_retries,
                max_json_repairs=s.max_json_repairs, check_key_info=s.check_key_info,
            )
        self.llm = llm
        self.cache = cache or AnalysisCache(s.runtime_cache_dir, s.bundled_cache_dir)
        fallbacks = [m for m in [s.fallback_model] if m and m != s.model]
        self.memory = memory
        if self.memory is None and s.memory_enabled and s.mode != "template":
            try:
                self.memory = MemoryStore(s.memory_path)
            except Exception as exc:  # noqa: BLE001 - без памяти сервис работает как раньше
                logger.warning("Память фактов недоступна: %s", type(exc).__name__)
        self.analyst = BusinessAnalyst(
            llm, model=s.model, cache=self.cache, max_tokens=s.max_tokens, max_parallel=s.max_parallel, fallback_models=fallbacks,
            prompt_version=s.prompt_version, memory=self.memory, fewshot=s.fewshot,
        )
        if bank_evidence is None:
            from app.services.seed_data import SHARED_EVIDENCE

            bank_evidence = list(SHARED_EVIDENCE)
        self.bank_corpus = bank_fragments_from_evidence(bank_evidence)
        self._lock = threading.Lock()
        self._latencies: deque[int] = deque(maxlen=200)
        self._qa_seen: dict[str, tuple[int, int, str, bool]] = {}
        self.fallbacks_to_template = 0

    # ------------------------------------------------------------------ режимы
    @property
    def mode(self) -> str:
        return self.settings.mode

    @property
    def enabled(self) -> bool:
        return self.settings.mode in ("llm", "cached")

    def live_status(self) -> tuple[bool, str]:
        if self.settings.mode != "llm":
            return False, f"режим {self.settings.mode}"
        if self.llm is None:
            return False, "ключ LLM_API_KEY не задан"
        return self.llm.available()

    def as_of(self) -> datetime:
        fixed = parse_dt(self.settings.as_of) if self.settings.as_of else None
        return fixed or datetime.now(MSK)

    # ------------------------------------------------------------------ вход
    def build_input(self, company: dict[str, Any], role: str, events: list[dict[str, Any]], score_fn: ScoreFn | None):
        relevance = {}
        if score_fn is not None:
            for ev in events:
                try:
                    relevance[str(ev.get("event_id"))] = score_fn(ev, company, role)
                except Exception:  # noqa: BLE001 - сигнал отбора необязателен
                    pass
        return build_analyst_input(
            company, role, events, as_of=self.as_of(), relevance_by_event=relevance, bank_corpus=self.bank_corpus,
            news_window_hours=self.settings.news_window_hours, max_insights=max(10, len(events)),
        )

    def _record(self, outcome: CandidateOutcome, started: float) -> None:
        with self._lock:
            self._latencies.append(int((time.perf_counter() - started) * 1000))
            key = outcome.run.cache_key or outcome.run.candidate_id
            ins = outcome.insight
            if ins is not None:
                stmts = list(ins.facts) + [s for s in (ins.company_relevance, ins.impact_mechanism, ins.ai_conclusion) if s]
                stmts += ins.risks + ins.opportunities + ins.next_steps
                with_refs = sum(1 for s in stmts if s.claim_ids or s.feature_ids or s.evidence_ids)
                self._qa_seen[key] = (len(stmts), with_refs, outcome.run.qa_status, ins.bank_assessment.status == "found")

    # ------------------------------------------------------------------ лента
    def feed(self, company: dict[str, Any], role: str, events: list[dict[str, Any]], score_fn: ScoreFn) -> list[FeedItem]:
        started = time.perf_counter()
        inp = self.build_input(company, role, events, score_fn)
        live, _ = self.live_status()
        output, results = self.analyst.analyze(inp, allow_live=live, prefer_cache=self.settings.prefer_cache)
        duplicates = {e.candidate_id for e in output.excluded_candidates if e.reason_code == "duplicate"}
        pv = inp.company_context.profile_version
        items: list[FeedItem] = []
        for ev, cand in zip(events, inp.candidates):
            res = results.get(cand.candidate_id)
            if isinstance(res, AnalysisUnavailable) or res is None:
                with self._lock:
                    self.fallbacks_to_template += 1
                rel = score_fn(ev, company, role)
                from app.schemas.base import NewsEvent

                items.append(FeedItem(event=NewsEvent.model_validate(ev), relevance=rel, why_short=rel.explanation,
                                      has_bank_position=False, profile_version=pv))
                continue
            self._record(res, started)
            uploaded = bool(ev.get("user_uploaded"))
            if not uploaded and (cand.candidate_id in duplicates or (res.insight is None and self.settings.feed_hide_excluded)):
                continue
            items.append(to_feed_item(res, ev, pv))
        items.sort(key=lambda it: it.relevance.score, reverse=True)
        return items

    # ------------------------------------------------------------------ карточка
    def run_id(self, company_id: str, role: str, cand_ref: str) -> str:
        return "an_" + key_hash(f"{company_id}|{role}|{cand_ref}|{self.analyst.prompt_version}")[:16]

    # ------------------------------------------------------------------ пакет ретривера
    def analyze_batch(self, raw_batch: dict[str, Any], role: str | None = None) -> tuple[AnalystOutput, Any]:
        """``ImpactCandidateBatch v1`` → ``AnalystOutput`` (и результат адаптера для трассы).

        Пустой пакет (``status=empty`` или без кандидатов) даёт пустой ответ без вызова LLM.
        """
        from app.analyst.batch_adapter import ImpactBatchAdapter

        adapter = ImpactBatchAdapter(prompt_version=self.settings.prompt_version,
                                     reserve_tokens=600 if self.memory is not None else 0)
        adapted = adapter.to_analyst_input(raw_batch, role=role)
        live, _ = self.live_status()
        output, _results = self.analyst.analyze(adapted.analyst_input, allow_live=live, prefer_cache=self.settings.prefer_cache)
        return output, adapted

    def insight(
        self,
        company: dict[str, Any],
        role: str,
        event: dict[str, Any],
        score_fn: ScoreFn,
        template_fn: Callable[[], Insight],
    ) -> Insight:
        started = time.perf_counter()
        inp = self.build_input(company, role, [event], score_fn)
        cand = inp.candidates[0]
        live, why = self.live_status()
        try:
            outcome = self.analyst.analyze_candidate(inp, cand, allow_live=live, prefer_cache=self.settings.prefer_cache)
        except AnalysisUnavailable as exc:
            with self._lock:
                self.fallbacks_to_template += 1
            insight = template_fn()
            insight.trace.append(
                TraceStep(
                    n=len(insight.trace) + 1, tool="llm_analyst",
                    summary="LLM-аналитик недоступен — показан шаблонный результат без LLM",
                    result=f"Причина: {exc if str(exc) else why}", status="warn",
                )
            )
            return insight
        self._record(outcome, started)
        ref = cand.event_bundle_refs[0]
        run_id = self.run_id(inp.request.company_id, role, f"{ref.event_id}@{ref.event_version}#{ref.snapshot_sha256[:12]}")
        return to_insight(outcome, event=event, cand=cand, ctx=inp.company_context, company_id=inp.request.company_id, role=role, run_id=run_id)

    # ------------------------------------------------------------------ чат
    @staticmethod
    def card_for_chat(insight: Insight) -> dict[str, Any]:
        def claims(items: list[Any]) -> list[dict[str, Any]]:
            return [
                {"text": c.text, "kind": c.kind, "sources": c.evidence_ids + [f"cf:{f}" for f in (c.features or [])]}
                for c in items
            ]

        return {
            "event": {"title": insight.event.title, "date": insight.event.date[:10]},
            "significance": {"level": insight.relevance.level, "score": insight.relevance.score},
            "what_happened": claims(insight.what_happened.facts),
            "why_it_matters": claims(insight.why_it_matters),
            "risks": claims(insight.risks),
            "opportunities": claims(insight.opportunities),
            "next_actions": claims(insight.next_actions),
            "impact_estimate": insight.impact_estimate.model_dump() if insight.impact_estimate else None,
            "bank_position": claims(insight.bank_position.claims) if insight.bank_position else "нет позиции банка по событию",
            "ai_conclusion": insight.ai_conclusion.text if insight.ai_conclusion else None,
            "unknowns": [u.text for u in insight.unknowns],
            "sources": [
                {"id": e.evidence_id, "type": e.type, "title": e.title, "publisher": e.publisher, "date": e.date, "quote": e.quote[:300]}
                for e in insight.evidence
            ],
        }

    def ask(
        self,
        company: dict[str, Any],
        role: str,
        event: dict[str, Any],
        question: str,
        score_fn: ScoreFn,
        template_insight_fn: Callable[[], Insight],
        template_answer_fn: Callable[[], QaAnswer],
    ) -> QaAnswer:
        insight = self.insight(company, role, event, score_fn, template_insight_fn)
        norm_q = " ".join(question.lower().split())[:400]
        key = f"chat|{insight.run_id}|{key_hash(norm_q)}|{prompts.CHAT_PROMPT_VERSION}|{self.settings.model}"
        record, _ = self.cache.get(key)
        if record is not None:
            answer = QaAnswer.model_validate(record)
            answer.from_cache = True
            return answer
        live, _ = self.live_status()
        if not live or self.llm is None:
            return template_answer_fn()
        card = self.card_for_chat(insight)
        by_id = {e.evidence_id: e for e in insight.evidence}
        request = ChatRequest(
            messages=prompts.build_chat_messages(card, question, role), model=self.settings.model,
            max_tokens=self.settings.chat_max_tokens, temperature=0.1, purpose="chat",
        )
        try:
            draft, usage = self.llm.chat_json(request, ChatDraft.model_validate)
        except LLMError as exc:
            logger.warning("Чат через LLM не выполнен: %s", exc)
            return template_answer_fn()
        ids = [i for i in dict.fromkeys(draft.evidence_ids) if i in by_id]
        insufficient = bool(draft.insufficient)
        text = draft.answer.strip()
        card_numbers = numbers_in(str(card))
        unsupported = {n for n in numbers_in(text) if n not in card_numbers}
        if unsupported:
            insufficient = True
            text += " (Часть чисел в ответе не подтверждена карточкой — проверьте по источникам.)"
        if not ids and not insufficient:
            insufficient = True
        answer = QaAnswer(
            text=text,
            evidence=[by_id[i] for i in ids if by_id[i].type != "company_feature"],
            evidence_ids=ids,
            did_extra_search=False,
            extra_search_note=f"Ответ по данным карточки ({usage.model}, {usage.cost_rub:.4f} ₽)",
            from_cache=False,
            insufficient=insufficient,
        )
        self.cache.put(key, answer.model_dump(mode="json"), label=f"chat/{insight.company_id}", model=self.settings.model,
                       created_at=datetime.now(MSK).isoformat(timespec="seconds"))
        return answer

    # ------------------------------------------------------------------ загрузка новости
    def classify_upload(self, title: str, text: str) -> dict[str, Any]:
        live, _ = self.live_status()
        if live and self.llm is not None:
            request = ChatRequest(
                messages=prompts.build_classify_messages(title, text), model=self.settings.model, max_tokens=200,
                temperature=0.0, purpose="classify",
            )
            try:
                draft, _usage = self.llm.chat_json(request, ClassifyDraft.model_validate)
                topic = draft.topic if draft.business_relevant else "other"
                return {"topic": topic, "topic_label": TOPIC_LABELS[topic], "summary": draft.summary, "classified_by": "llm"}
            except LLMError as exc:
                logger.warning("Классификация новости через LLM не выполнена: %s", exc)
        joined = f"{title} {text}"
        for topic, pattern in TOPIC_KEYWORDS:
            if re.search(pattern, joined, flags=re.I):
                return {"topic": topic, "topic_label": TOPIC_LABELS[topic], "summary": None, "classified_by": "rules"}
        return {"topic": "other", "topic_label": TOPIC_LABELS["other"], "summary": None, "classified_by": "rules"}

    # ------------------------------------------------------------------ метрики
    def refresh_team_spend(self) -> None:
        if isinstance(self.llm, LLMGateway):
            self.llm.refresh_remote_spend()

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            seen = list(self._qa_seen.values())
            lat = list(self._latencies)
            fallbacks = self.fallbacks_to_template
        total = sum(x[0] for x in seen)
        with_refs = sum(x[1] for x in seen)
        live, why = self.live_status()
        return {
            "analyst_mode": self.mode,
            "analyst_model": self.settings.model if self.mode != "template" else None,
            "analyst_fallback_model": self.settings.fallback_model if self.mode != "template" else None,
            "prompt_version": self.analyst.prompt_version,
            "memory_enabled": self.memory is not None,
            "live_llm_available": live,
            "live_llm_status": why,
            "budget": self.ledger.snapshot(),
            "llm": self.stats.snapshot(),
            "cache": self.cache.snapshot(),
            "insights_analyzed": len(seen),
            "claim_source_attribution_ratio": round(with_refs / total, 3) if total else None,
            "qa_failed": sum(1 for x in seen if x[2] == "failed"),
            "bank_position_found": sum(1 for x in seen if x[3]),
            "average_latency_ms": round(sum(lat) / len(lat)) if lat else None,
            "fallbacks_to_template": fallbacks,
        }


_service: AnalystService | None = None
_service_lock = threading.Lock()


def get_analyst_service() -> AnalystService:
    global _service
    with _service_lock:
        if _service is None:
            _service = AnalystService()
        return _service


def set_analyst_service(service: AnalystService | None) -> None:
    """Подмена сервиса (тесты, скрипты)."""
    global _service
    with _service_lock:
        _service = service


__all__ = ["AnalystService", "get_analyst_service", "set_analyst_service", "relevance_from", "TOPIC_LABELS"]
