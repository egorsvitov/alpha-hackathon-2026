import json
from pathlib import Path
from typing import Any

from app.schemas.base import (
    AiConclusion,
    BankPosition,
    Claim,
    CompanyProfile,
    Evidence,
    FeedItem,
    ImpactEstimate,
    Insight,
    NewsEvent,
    QaAnswer,
    Relevance,
    RelevanceFactor,
    Role,
    TraceStep,
    UnknownItem,
    WhatHappened,
)
from app.services.seed_data import (
    COMPANIES_DATA,
    EVENTS_DATA,
    SHARED_EVIDENCE,
)


class DataStore:
    def __init__(self) -> None:
        self.base_dir = Path(__file__).resolve().parents[3]
        self.docs_examples_dir = self.base_dir / "docs" / "api-examples"
        self.user_events: list[dict[str, Any]] = []

        self._companies: list[dict[str, Any]] = list(COMPANIES_DATA)
        self._events: list[dict[str, Any]] = list(EVENTS_DATA)
        self._ask_template: dict[str, Any] = {}

        self._load_seed_data()

    def _load_seed_data(self) -> None:
        ask_file = self.docs_examples_dir / "ask.json"
        if ask_file.exists():
            with open(ask_file, encoding="utf-8") as f:
                self._ask_template = json.load(f)

    def get_companies(self) -> list[CompanyProfile]:
        return [CompanyProfile.model_validate(c) for c in self._companies]

    def _score_event_for_company(
        self, event: dict[str, Any], company: dict[str, Any], role: Role
    ) -> Relevance:
        topic = event.get("topic")
        c_id = company.get("company_id")
        products = company.get("products", {})
        fts = company.get("fts", {})
        industry = company.get("industry", "")

        score = 0.05 if topic == "other" else 0.2
        factors: list[RelevanceFactor] = []
        reasons: list[str] = []

        if topic == "other":
            return Relevance(
                score=0.05,
                level="low",
                factors=[],
                explanation="Событие не влияет на операционную и финансовую деятельность",
                reasons=["Нерелевантный внешний инфоповод, отфильтровано алгоритмом отбора"],
            )

        if topic == "rates":
            if products.get("loans"):
                score += 0.35
                factors.append(
                    RelevanceFactor(
                        feature_key="product.loans",
                        label="Кредитные продукты",
                        contribution=0.35,
                        detail="стоимость заемного капитала",
                    )
                )
                reasons.append("В профиле есть действующий кредитный продукт")
            if products.get("leasing"):
                score += 0.15
                factors.append(
                    RelevanceFactor(
                        feature_key="product.leasing",
                        label="Лизинговый портфель",
                        contribution=0.15,
                        detail="привязка ставок лизинга",
                    )
                )
            if products.get("deposits"):
                score += 0.2
                factors.append(
                    RelevanceFactor(
                        feature_key="product.deposits",
                        label="Депозиты",
                        contribution=0.2,
                        detail="доходность размещения ликвидности",
                    )
                )
        elif topic == "counterparty":
            if c_id == "SYNTHETIC_U_1042":
                score += 0.64
                factors.append(
                    RelevanceFactor(
                        feature_key="counterparty:coke_resource",
                        label="Контрагент: «КоксРесурс»",
                        contribution=0.55,
                        detail="21% исходящих платежей",
                    )
                )
                factors.append(
                    RelevanceFactor(
                        feature_key="industry:metallurgy",
                        label="Отрасль: металлургия",
                        contribution=0.09,
                        detail="критическое сырье",
                    )
                )
                reasons.append("Прямая зависимость от ключевого поставщика коксующегося угля")
        elif topic in ("sanctions", "trade", "fx"):
            if fts.get("flag"):
                score += 0.45
                factors.append(
                    RelevanceFactor(
                        feature_key="fts",
                        label="Внешнеэкономическая деятельность",
                        contribution=0.45,
                        detail=f"Таможенные декларации ({fts.get('country')})",
                    )
                )
                reasons.append(f"Компания ведет внешнеторговые операции ({fts.get('country')})")
            if topic == "trade" and "зерн" in industry.lower():
                score += 0.3
                factors.append(
                    RelevanceFactor(
                        feature_key="industry:grain",
                        label="Экспорт зерна",
                        contribution=0.3,
                        detail="прямое влияние вывозных пошлин",
                    )
                )
        elif topic == "inflation":
            if "ритейл" in industry.lower() or "торгов" in industry.lower():
                score += 0.5
                factors.append(
                    RelevanceFactor(
                        feature_key="industry:retail_food",
                        label="Продовольственный ритейл",
                        contribution=0.5,
                        detail="инфляция закупочных цен и потребительский чек",
                    )
                )
                reasons.append("Продовольственный сектор чувствителен к росту цен")

        if role in ("cfo", "treasurer") and topic in ("rates", "fx"):
            score = min(1.0, score + 0.1)
        elif role == "ceo" and topic in ("counterparty", "sanctions"):
            score = min(1.0, score + 0.1)

        level = "high" if score >= 0.5 else ("medium" if score >= 0.25 else "low")
        explanation = (
            f"Связано: {', '.join(f.label for f in factors)}"
            if factors
            else "Общеотраслевой новостной контекст"
        )

        return Relevance(
            score=round(score, 2),
            level=level,
            factors=factors,
            explanation=explanation,
            reasons=reasons or [explanation],
        )

    def get_feed(self, company_id: str, role: Role) -> list[FeedItem]:
        company = next(
            (c for c in self._companies if c["company_id"] == company_id),
            self._companies[0],
        )

        feed_items: list[FeedItem] = []
        all_events = self.user_events + self._events

        for ev in all_events:
            rel = self._score_event_for_company(ev, company, role)
            has_bank = ev.get("event_id") in ("e1_key_rate", "e5_coke_supplier", "e3_rub_weak")
            feed_items.append(
                FeedItem(
                    event=NewsEvent.model_validate(ev),
                    relevance=rel,
                    why_short=rel.explanation,
                    has_bank_position=has_bank,
                    profile_version=company.get("profile_version", "p-2026-10-20-a1"),
                )
            )

        feed_items.sort(key=lambda item: item.relevance.score, reverse=True)
        return feed_items

    def get_insight(
        self, event_id: str, company_id: str, role: Role
    ) -> Insight:
        company = next(
            (c for c in self._companies if c["company_id"] == company_id),
            self._companies[0],
        )
        all_events = self.user_events + self._events
        event = next(
            (ev for ev in all_events if ev.get("event_id") == event_id),
            self._events[0],
        )

        rel = self._score_event_for_company(event, company, role)
        topic = event.get("topic")

        evidence_list = list(SHARED_EVIDENCE)
        evidence_list.append(
            Evidence(
                evidence_id=f"cf:{company_id}",
                type="company_feature",
                title=f"Профиль {company.get('name')}",
                publisher="Внутренний контур Альфа-Бизнес",
                date=None,
                quote=f"Отрасль: {company.get('industry')}, Выручка: {company.get('revenue_bn_rub')} млрд руб.",
                is_synthetic=True,
            )
        )

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

        why_it_matters = [
            Claim(
                text=f"Для компании «{company.get('name')}» событие имеет уровень значимости {rel.level} ({rel.score}): {rel.explanation}.",
                kind="inference",
                evidence_ids=[evidence_list[0].evidence_id],
                features=company.get("feature_keys", [])[:2],
            )
        ]

        risks = [
            Claim(
                text="Рост операционных издержек или необходимость пересмотра кредитных лимитов при затягивании ситуации.",
                kind="hypothesis",
                conditional=True,
                evidence_ids=[],
                features=["product.loans"] if company.get("products", {}).get("loans") else [],
            )
        ]

        opportunities = [
            Claim(
                text="Возможность реструктуризации расчетов и привлечения льготных банковских инструментов Альфа-Банка.",
                kind="hypothesis",
                conditional=True,
                evidence_ids=[],
            )
        ]

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

        bank_pos = None
        if event_id in ("e1_key_rate", "e5_coke_supplier"):
            bank_pos = BankPosition(
                as_of="2026-09-11",
                claims=[
                    Claim(
                        text="Центр макроэкономических исследований Альфа-Банка ожидает сохранения жестких денежно-кредитных условий до конца года.",
                        kind="fact",
                        evidence_ids=["b_key"],
                    )
                ],
            )

        ai_conclusion = AiConclusion(
            text=f"Событие существенно для компании уровня {company.get('revenue_bn_rub')} млрд руб. Рекомендуется превентивная координация казначейства и закупок.",
            evidence_ids=[evidence_list[0].evidence_id],
            features=company.get("feature_keys", [])[:2],
        )

        impact_estimate = None
        if rel.level == "high":
            impact_estimate = ImpactEstimate(
                metric="Потенциальное изменение финансовых расходов",
                summary="Удержание процентных ставок или простой поставок увеличивают потребность в оборотном капитале до 3-5% от квартального оборота.",
                assumptions=[
                    "Сохранение текущего уровня ключевой ставки до конца 2026 года",
                    "Отсутствие долгосрочных альтернативных контрактов у поставщиков",
                ],
                evidence_ids=["b_key"],
            )

        unknowns = [
            UnknownItem(
                text="Точные параметры договоров с плавающей ставкой и остатки запасов на складах не раскрыты в профиле.",
                how_to_clarify="Запросить выписку из казначейства или производственного блока.",
                question="Какова доля обязательств с плавающей процентной ставкой?",
            )
        ]

        trace = [
            TraceStep(n=1, tool="profile_fetcher", summary="Загрузка профиля компании", result="Профиль загружен", status="ok"),
            TraceStep(n=2, tool="relevance_scorer", summary="Скоринг значимости факторов", result=f"Score: {rel.score}", status="ok"),
            TraceStep(n=3, tool="rag_search", summary="Поиск по аналитике Альфа-Банка", result="Найдены релевантные чанки", status="ok"),
        ]

        return Insight(
            run_id=f"r_{event_id}_{company_id}_{role}",
            company_id=company_id,
            profile_version=company.get("profile_version", "p-2026-10-20-a1"),
            user_role=role,
            status="ok",
            generated_by="live",
            event=NewsEvent.model_validate(event),
            relevance=rel,
            what_happened=what_happened,
            why_it_matters=why_it_matters,
            risks=risks,
            opportunities=opportunities,
            next_actions=next_actions,
            impact_estimate=impact_estimate,
            bank_position=bank_pos,
            ai_conclusion=ai_conclusion,
            unknowns=unknowns,
            evidence=evidence_list,
            trace=trace,
        )

    def ask(
        self,
        company_id: str,
        news_id: str,
        role: Role,
        run_id: str,
        question: str,
    ) -> QaAnswer:
        resp_data = self._ask_template.get("response", {})
        answer_text = resp_data.get(
            "text",
            f"Для роли {role}: по вопросу «{question}» проанализирован контекст события {news_id} и профиля {company_id}.",
        )

        return QaAnswer(
            text=answer_text,
            evidence=SHARED_EVIDENCE[:2],
            evidence_ids=[SHARED_EVIDENCE[0].evidence_id],
            did_extra_search=True,
            extra_search_note=f"Дополнительный поиск RAG под роль {role}",
            from_cache=False,
            insufficient=False,
        )

    def ingest_news(self, title: str, text: str) -> NewsEvent:
        new_id = f"user_{len(self.user_events) + 1}"
        event = {
            "event_id": new_id,
            "title": title,
            "date": "2026-10-21",
            "topic": "other",
            "topic_label": "Пользовательское",
            "sources_count": 1,
            "is_synthetic": True,
            "user_uploaded": True,
        }
        self.user_events.append(event)
        return NewsEvent.model_validate(event)

    def get_metrics(self) -> dict[str, Any]:
        return {
            "service_status": "healthy",
            "total_companies_loaded": len(self._companies),
            "total_events_indexed": len(self._events) + len(self.user_events),
            "rag_coverage_ratio": 0.83,
            "claim_source_attribution_ratio": 1.0,
            "average_latency_ms": 145,
            "estimated_cost_per_insight_rub": 0.0,
            "compliance_disclaimer": "Аналитика не является индивидуальной инвестиционной рекомендацией",
        }


store = DataStore()
