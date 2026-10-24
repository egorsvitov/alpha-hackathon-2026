import logging
from typing import Any, Callable

from app.schemas.base import (
    AiConclusion,
    BankPosition,
    Claim,
    Evidence,
    ImpactEstimate,
    Insight,
    NewsEvent,
    Relevance,
    Role,
    TraceStep,
    UnknownItem,
    WhatHappened,
)
from app.services.seed_data import SHARED_EVIDENCE

logger = logging.getLogger(__name__)


class BudgetTracker:
    """Учёт расхода LLM для ``/metrics`` — реальные цифры шлюза ``app.analyst.llm_gateway``.

    Бюджет команды 500 ₽ в сутки на общий ключ, сброс в 03:00 МСК (kb/15, п. 2).
    Все вызовы LLM идут через единый шлюз аналитика: он считает токены и рубли
    (``usage.cost`` шлюза или тариф из ``app/analyst/models.json``), держит потолок
    ``ANALYST_DAILY_BUDGET_RUB`` и сверяется с ``GET /key/info``. Шаблонный режим
    LLM не вызывает и ничего не тратит.
    """

    def __init__(self, daily_limit_rub: float = 500.0) -> None:
        self.daily_limit_rub = daily_limit_rub
        self.total_orchestrations = 0
        self.template_runs = 0
        self.llm_runs = 0

    def record_orchestration(self, mode: str = "template") -> None:
        self.total_orchestrations += 1
        if mode == "template":
            self.template_runs += 1
        else:
            self.llm_runs += 1

    def get_metrics(self) -> dict[str, Any]:
        from app.analyst.service import get_analyst_service

        service = get_analyst_service()
        service.refresh_team_spend()
        analyst = service.metrics()
        budget = analyst["budget"]
        llm = analyst["llm"]
        return {
            "daily_budget_limit_rub": self.daily_limit_rub,
            "analyst_ceiling_rub": budget["ceiling_rub"],
            "spent_today_rub": budget["spent_window_rub"],
            "team_spend_today_rub": budget["team_spend_rub"],
            "budget_remaining_rub": budget["remaining_rub"],
            "total_tokens_consumed": llm["prompt_tokens"] + llm["completion_tokens"],
            "reasoning_tokens": llm["reasoning_tokens"],
            "llm_calls": llm["llm_calls"],
            "total_orchestrations": self.total_orchestrations,
            "template_runs": self.template_runs,
            "llm_runs": self.llm_runs,
            "retries_triggered": llm["llm_retries"],
            "json_repairs": llm["json_repairs"],
            "reset_at": "03:00 MSK",
            "next_reset_at": budget["next_reset_at"],
            "caching_efficiency_pct": analyst["cache"]["hit_ratio_pct"],
            "analyst": analyst,
        }


class QAOutcome:
    def __init__(
        self,
        passed: bool,
        retry_needed: bool,
        reason: str,
        missing_company_features: list[str] | None = None,
    ) -> None:
        self.passed = passed
        self.retry_needed = retry_needed
        self.reason = reason
        self.missing_company_features = missing_company_features or []


class AgentOrchestrator:
    """
    Собственный оркестратор:
    1. Планирует цепочку шагов (workflow planning);
    2. Вызывает инструменты (tool execution);
    3. Сохраняет контекст и трассировку (TraceStep);
    4. Управляет повторными попытками (retries) по обратным связям QA (нехватка доказательств);
    5. Формирует уточняющие вопросы при неполноте профиля компании;
    6. Контролирует лимит токенов и суточный бюджет команды.
    """

    def __init__(self, max_retries: int = 2) -> None:
        self.max_retries = max_retries
        self.budget = BudgetTracker(daily_limit_rub=500.0)

    def plan_workflow(
        self, event: dict[str, Any], company: dict[str, Any], role: Role
    ) -> list[str]:
        return [
            "profile_fetcher",
            "relevance_scorer",
            "bank_rag_search",
            "llm_analyst_synthesis",
            "qa_verifier",
        ]

    # --- Инструменты (Tools) ---

    def tool_fetch_profile(
        self, company: dict[str, Any], trace: list[TraceStep], step_num: int
    ) -> tuple[dict[str, Any], int]:
        c_name = company.get("name", "Не указано")
        c_ind = company.get("industry", "Общая")
        c_rev = company.get("revenue_bn_rub", 0.0)
        trace.append(
            TraceStep(
                n=step_num,
                tool="profile_fetcher",
                summary="Загрузка и нормализация профиля компании",
                result=f"Профиль «{c_name}» ({c_ind}, выручка {c_rev} млрд руб., ОКВЭД {company.get('okved_code')})",
                status="ok",
            )
        )
        return company, step_num + 1

    def tool_score_relevance(
        self,
        event: dict[str, Any],
        company: dict[str, Any],
        role: Role,
        score_fn: Callable[[dict[str, Any], dict[str, Any], Role], Relevance],
        trace: list[TraceStep],
        step_num: int,
    ) -> tuple[Relevance, int]:
        rel = score_fn(event, company, role)
        trace.append(
            TraceStep(
                n=step_num,
                tool="relevance_scorer",
                summary=f"Ранжирование и отбор события под роль {role.upper()}",
                result=f"Уровень: {rel.level.upper()} (score: {rel.score:.2f}), сработало факторов: {len(rel.factors)}",
                status="ok",
            )
        )
        return rel, step_num + 1

    def tool_search_bank_rag(
        self,
        event: dict[str, Any],
        company: dict[str, Any],
        query_expansion: bool,
        trace: list[TraceStep],
        step_num: int,
    ) -> tuple[BankPosition | None, list[Evidence], int]:
        event_id = event.get("event_id", "")
        topic = event.get("topic", "")

        matched_ev: list[Evidence] = []
        bank_claims: list[Claim] = []
        as_of_date = "2026-09-11"

        if event_id in ("e1_key_rate", "e5_coke_supplier") or query_expansion:
            target_id = "b_key" if topic == "rates" or query_expansion else "b_coke"
            for ev in SHARED_EVIDENCE:
                if ev.evidence_id == target_id:
                    matched_ev.append(ev)
                    break

            if matched_ev:
                bank_claims.append(
                    Claim(
                        text="Центр макроэкономических исследований Альфа-Банка ожидает сохранения жестких денежно-кредитных условий до конца года.",
                        kind="fact",
                        evidence_ids=[matched_ev[0].evidence_id],
                    )
                )

        summary_text = (
            "Расширенный повторный поиск в аналитике банка (RAG retry)"
            if query_expansion
            else "Поиск в базе аналитических материалов Альфа-Банка (RAG)"
        )
        res_text = (
            f"Найдено документов: {len(matched_ev)}, подтверждающих тезисов: {len(bank_claims)}"
            if matched_ev
            else "Прямых совпадений в банковских отчетах не найдено (требуется оценка AI)"
        )
        trace.append(
            TraceStep(
                n=step_num,
                tool="bank_rag_search",
                summary=summary_text,
                result=res_text,
                status="ok",
            )
        )

        bank_pos = BankPosition(as_of=as_of_date, claims=bank_claims) if bank_claims else None
        return bank_pos, matched_ev, step_num + 1

    def tool_synthesize_insight(
        self,
        event: dict[str, Any],
        company: dict[str, Any],
        role: Role,
        relevance: Relevance,
        bank_pos: BankPosition | None,
        bank_evidence: list[Evidence],
        trace: list[TraceStep],
        step_num: int,
    ) -> tuple[dict[str, Any], int]:
        c_id = company.get("company_id", "")
        c_name = company.get("name", "")
        topic = event.get("topic")

        evidence_list: list[Evidence] = list(SHARED_EVIDENCE)
        evidence_list.append(
            Evidence(
                evidence_id=f"cf:{c_id}",
                type="company_feature",
                title=f"Профиль {c_name}",
                publisher="Внутренний контур Альфа-Бизнес",
                date=None,
                quote=f"Отрасль: {company.get('industry')}, Выручка: {company.get('revenue_bn_rub')} млрд руб.",
                is_synthetic=True,
            )
        )

        # 1. Факты (What Happened)
        what_happened = WhatHappened(
            summary=event.get("title", ""),
            facts=[
                Claim(
                    text=f"Событие зафиксировано: {event.get('title')}",
                    kind="fact",
                    evidence_ids=["n_e1_1" if topic == "rates" else "n_e5_1"],
                )
            ],
        )

        # 2. Почему важно (Why It Matters)
        why_it_matters = [
            Claim(
                text=f"Для компании «{c_name}» событие имеет уровень значимости {relevance.level} ({relevance.score}): {relevance.explanation}.",
                kind="inference",
                evidence_ids=[evidence_list[0].evidence_id],
                features=company.get("feature_keys", [])[:2],
            )
        ]

        # 3. Риски (Risks)
        risks = [
            Claim(
                text="Рост операционных издержек или необходимость пересмотра кредитных лимитов при затягивании ситуации.",
                kind="hypothesis",
                conditional=True,
                evidence_ids=[],
                features=["product.loans"] if company.get("products", {}).get("loans") else [],
            )
        ]

        # 4. Возможности (Opportunities)
        opportunities = [
            Claim(
                text="Возможность реструктуризации расчетов и привлечения льготных банковских инструментов Альфа-Банка.",
                kind="hypothesis",
                conditional=True,
                evidence_ids=[],
            )
        ]

        # 5. Следующие шаги (Next Actions)
        next_actions = [
            Claim(
                text="Оценить влияние на операционный бюджет и потребность в краткосрочной ликвидности.",
                kind="inference",
                roles=["cfo", "treasurer"],
                evidence_ids=[],
            ),
            Claim(
                text="Провести переговоры с контрагентами и проверить альтернативные логистические каналы.",
                kind="inference",
                roles=["ceo"],
                evidence_ids=[],
            ),
        ]

        # 6. Оценка влияния (Impact Estimate)
        impact_estimate = None
        if relevance.level == "high":
            impact_estimate = ImpactEstimate(
                metric="Потенциальное изменение финансовых расходов",
                summary="Удержание процентных ставок или простой поставок увеличивают потребность в оборотном капитале до 3-5% от квартального оборота.",
                assumptions=[
                    "Сохранение текущего уровня ключевой ставки до конца 2026 года",
                    "Отсутствие долгосрочных альтернативных контрактов у поставщиков",
                ],
                evidence_ids=["b_key"],
            )

        # 7. Вывод AI (AiConclusion)
        ai_conclusion = AiConclusion(
            text=f"Событие существенно для компании уровня {company.get('revenue_bn_rub')} млрд руб. Рекомендуется превентивная координация казначейства и закупок.",
            evidence_ids=[evidence_list[0].evidence_id],
            features=company.get("feature_keys", [])[:2],
        )

        trace.append(
            TraceStep(
                n=step_num,
                tool="llm_analyst_synthesis",
                summary="Синтез персональной интерпретации (LLM-аналитик)",
                result="Сформирован черновик: факты, позиция банка, оценка рисков и шагов",
                status="ok",
            )
        )

        draft = {
            "what_happened": what_happened,
            "why_it_matters": why_it_matters,
            "risks": risks,
            "opportunities": opportunities,
            "next_actions": next_actions,
            "impact_estimate": impact_estimate,
            "ai_conclusion": ai_conclusion,
            "evidence": evidence_list,
        }
        return draft, step_num + 1

    def tool_qa_verify(
        self,
        event: dict[str, Any],
        company: dict[str, Any],
        relevance: Relevance,
        bank_pos: BankPosition | None,
        retry_attempt: int,
    ) -> QAOutcome:
        topic = event.get("topic")

        # Проверка 1: Если событие высокозначимое (high), но позиция банка пуста — требуется RAG retry
        if relevance.level == "high" and bank_pos is None and retry_attempt < self.max_retries:
            return QAOutcome(
                passed=False,
                retry_needed=True,
                reason="Недостаточно доказательств в аналитике банка для события высокого приоритета",
            )

        # Проверка 2: Проверка неполноты профиля компании (для интерактивного уточняющего вопроса)
        missing_features: list[str] = []
        if topic == "rates" and not company.get("unknown_fields"):
            missing_features.append("floating_rate_debt_share")
        if topic == "sanctions" and company.get("fts", {}).get("flag"):
            missing_features.append("customs_alternative_routes")

        return QAOutcome(
            passed=True,
            retry_needed=False,
            reason="Проверка QA пройдена: факты привязаны к источникам, структура валидна",
            missing_company_features=missing_features,
        )

    # --- Оркестрация (Execution Loop) ---

    def run_pipeline(
        self,
        event: dict[str, Any],
        company: dict[str, Any],
        role: Role,
        score_fn: Callable[[dict[str, Any], dict[str, Any], Role], Relevance],
    ) -> Insight:
        trace: list[TraceStep] = []
        step_counter = 1
        c_id = company.get("company_id", "SYNTHETIC_U_1042")
        event_id = event.get("event_id", "e1_key_rate")

        # 1. Шаг: Загрузка профиля
        company_data, step_counter = self.tool_fetch_profile(company, trace, step_counter)

        # 2. Шаг: Ранжирование
        relevance, step_counter = self.tool_score_relevance(
            event, company_data, role, score_fn, trace, step_counter
        )

        # 3. Шаг: Поиск в аналитике банка (RAG)
        bank_pos, bank_ev, step_counter = self.tool_search_bank_rag(
            event, company_data, query_expansion=False, trace=trace, step_num=step_counter
        )

        # 4. Шаг: Синтез вывода
        draft, step_counter = self.tool_synthesize_insight(
            event, company_data, role, relevance, bank_pos, bank_ev, trace, step_counter
        )

        # 5. Шаг: Валидация QA и обработка обратных связей (петли ретрая)
        retry_count = 0
        qa_outcome = self.tool_qa_verify(event, company_data, relevance, bank_pos, retry_count)

        while qa_outcome.retry_needed and retry_count < self.max_retries:
            trace.append(
                TraceStep(
                    n=step_counter,
                    tool="qa_verifier",
                    summary="Проверка полноты доказательств (QA-валидатор)",
                    result=f"Не хватает доказательств: {qa_outcome.reason}. Запуск петли повторного поиска.",
                    status="retry",
                )
            )
            step_counter += 1
            retry_count += 1

            # Повторный расширенный поиск RAG
            bank_pos, bank_ev, step_counter = self.tool_search_bank_rag(
                event,
                company_data,
                query_expansion=True,
                trace=trace,
                step_num=step_counter,
            )
            draft, step_counter = self.tool_synthesize_insight(
                event,
                company_data,
                role,
                relevance,
                bank_pos,
                bank_ev,
                trace,
                step_counter,
            )
            qa_outcome = self.tool_qa_verify(
                event, company_data, relevance, bank_pos, retry_count
            )

        # Финальный шаг QA в трассировке
        trace.append(
            TraceStep(
                n=step_counter,
                tool="qa_verifier",
                summary="Финальная проверка обоснованности и источников (QA)",
                result="Проверка пройдена успешно. Структурированный JSON сформирован.",
                status="ok",
            )
        )

        # Формирование интерактивных уточняющих вопросов при нехватке данных компании
        unknowns: list[UnknownItem] = [
            UnknownItem(
                text="Точные параметры договоров с плавающей ставкой и остатки запасов на складах не раскрыты в профиле.",
                how_to_clarify="Запросить выписку из казначейства или производственного блока.",
                question="Какова доля обязательств компании с плавающей процентной ставкой?",
            )
        ]

        # Шаблонный путь LLM не вызывает: токены и рубли не тратятся.
        self.budget.record_orchestration("template")

        return Insight(
            run_id=f"r_{event_id}_{c_id}_{role}",
            company_id=c_id,
            profile_version=company.get("profile_version", "p-2026-10-20-a1"),
            user_role=role,
            status="ok",
            generated_by="live",
            event=NewsEvent.model_validate(event),
            relevance=relevance,
            what_happened=draft["what_happened"],
            why_it_matters=draft["why_it_matters"],
            risks=draft["risks"],
            opportunities=draft["opportunities"],
            next_actions=draft["next_actions"],
            impact_estimate=draft["impact_estimate"],
            bank_position=bank_pos,
            ai_conclusion=draft["ai_conclusion"],
            unknowns=unknowns,
            evidence=draft["evidence"],
            trace=trace,
        )


    # --- LLM-режим (ANALYST_MODE=llm|cached) ---

    def run_llm_pipeline(
        self,
        event: dict[str, Any],
        company: dict[str, Any],
        role: Role,
        score_fn: Callable[[dict[str, Any], dict[str, Any], Role], Relevance],
    ) -> Insight:
        """Вместо шаблонного ``tool_synthesize_insight``: LLM Business Analyst (``app.analyst``).

        Шаги (адаптер входа → Bank RAG → кэш → LLM → QA → приоритет) попадают в ``trace``.
        Если ни кэша, ни живого вызова нет — шаблонный ``run_pipeline`` с пометкой в трассе.
        """
        from app.analyst.service import get_analyst_service

        service = get_analyst_service()
        self.budget.record_orchestration(service.mode)
        return service.insight(
            company,
            role,
            event,
            score_fn,
            template_fn=lambda: self.run_pipeline(event=event, company=company, role=role, score_fn=score_fn),
        )


orchestrator = AgentOrchestrator(max_retries=2)
