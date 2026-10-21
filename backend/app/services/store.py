import json
from pathlib import Path
from typing import Any

from app.schemas.base import (
    CompanyProfile,
    FeedItem,
    Insight,
    NewsEvent,
    QaAnswer,
    Role,
)


class DataStore:
    def __init__(self) -> None:
        self.base_dir = Path(__file__).resolve().parents[3]
        self.docs_examples_dir = self.base_dir / "docs" / "api-examples"
        self.user_events: list[dict[str, Any]] = []

        self._companies: list[dict[str, Any]] = []
        self._feed: list[dict[str, Any]] = []
        self._analysis: dict[str, Any] = {}
        self._ask: dict[str, Any] = {}

        self._load_seed_data()

    def _load_seed_data(self) -> None:
        companies_file = self.docs_examples_dir / "companies.json"
        if companies_file.exists():
            with open(companies_file, encoding="utf-8") as f:
                self._companies = json.load(f)

        feed_file = self.docs_examples_dir / "feed.json"
        if feed_file.exists():
            with open(feed_file, encoding="utf-8") as f:
                self._feed = json.load(f)

        analysis_file = self.docs_examples_dir / "analysis.json"
        if analysis_file.exists():
            with open(analysis_file, encoding="utf-8") as f:
                self._analysis = json.load(f)

        ask_file = self.docs_examples_dir / "ask.json"
        if ask_file.exists():
            with open(ask_file, encoding="utf-8") as f:
                self._ask = json.load(f)

    def get_companies(self) -> list[CompanyProfile]:
        return [CompanyProfile.model_validate(c) for c in self._companies]

    def get_feed(self, company_id: str, role: Role) -> list[FeedItem]:
        items: list[dict[str, Any]] = list(self._feed)

        for u in self.user_events:
            items.insert(
                0,
                {
                    "event": u,
                    "relevance": {
                        "score": 0.75,
                        "level": "high",
                        "factors": [
                            {
                                "feature_key": "meta:user_upload",
                                "label": "Загружено пользователем",
                                "contribution": 0.75,
                                "detail": "событие добавлено вручную",
                            }
                        ],
                        "explanation": "Событие добавлено из интерфейса",
                        "reasons": ["Пользовательская загрузка"],
                    },
                    "why_short": "Событие добавлено из интерфейса",
                    "has_bank_position": False,
                    "profile_version": "p-2026-10-20-a1",
                },
            )

        def role_sort_key(item: dict[str, Any]) -> float:
            score = float(item.get("relevance", {}).get("score", 0.0))
            topic = item.get("event", {}).get("topic")
            if role in ("cfo", "treasurer") and topic in ("rates", "fx"):
                score += 0.1
            elif role == "ceo" and topic in ("counterparty", "sanctions"):
                score += 0.1
            return score

        sorted_items = sorted(items, key=role_sort_key, reverse=True)
        return [FeedItem.model_validate(it) for it in sorted_items]

    def get_insight(
        self, event_id: str, company_id: str, role: Role
    ) -> Insight:
        data = dict(self._analysis)
        data["company_id"] = company_id
        data["user_role"] = role

        if (
            "event" in data
            and data["event"].get("event_id") != event_id
        ):
            matching_event = next(
                (
                    item["event"]
                    for item in self._feed
                    if item.get("event", {}).get("event_id") == event_id
                ),
                None,
            )
            if not matching_event:
                matching_event = next(
                    (u for u in self.user_events if u.get("event_id") == event_id),
                    None,
                )

            if matching_event:
                data["event"] = matching_event
                data["run_id"] = f"r_{event_id}_{role}"
                data["what_happened"] = {
                    "summary": matching_event["title"],
                    "facts": [
                        {
                            "text": matching_event["title"],
                            "evidence_ids": ["n_e1_1"],
                        }
                    ],
                }

        return Insight.model_validate(data)

    def ask(
        self,
        company_id: str,
        news_id: str,
        role: Role,
        run_id: str,
        question: str,
    ) -> QaAnswer:
        resp_data = self._ask.get("response", {})
        answer_data = dict(resp_data)

        if not answer_data:
            answer_data = {
                "text": f"Позиция для роли {role}: вопрос «{question}» проанализирован.",
                "evidence": [],
                "evidence_ids": [],
                "did_extra_search": False,
                "from_cache": False,
                "insufficient": False,
            }

        return QaAnswer.model_validate(answer_data)

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


store = DataStore()
