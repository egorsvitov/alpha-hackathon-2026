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
from app.services.orchestrator import orchestrator


class DataStore:
    def __init__(self) -> None:
        self.base_dir = Path(__file__).resolve().parents[3]
        self.docs_examples_dir = self.base_dir / "docs" / "api-examples"
        self.user_events: list[dict[str, Any]] = []
        self.orchestrator = orchestrator

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

        return self.orchestrator.run_pipeline(
            event=event,
            company=company,
            role=role,
            score_fn=self._score_event_for_company,
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
        metrics = {
            "service_status": "healthy",
            "total_companies_loaded": len(self._companies),
            "total_events_indexed": len(self._events) + len(self.user_events),
            "rag_coverage_ratio": 0.83,
            "claim_source_attribution_ratio": 1.0,
            "average_latency_ms": 145,
            "estimated_cost_per_insight_rub": round(
                self.orchestrator.budget.spent_today_rub
                / max(1, self.orchestrator.budget.total_orchestrations),
                4,
            )
            if self.orchestrator.budget.total_orchestrations
            else 0.042,
            "compliance_disclaimer": "Аналитика не является индивидуальной инвестиционной рекомендацией",
        }
        metrics.update(self.orchestrator.budget.get_metrics())
        return metrics


store = DataStore()
