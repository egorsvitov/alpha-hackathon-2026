"""Оркестратор B2+B3: батч статей → утверждения + каталог событий.

Реализует три операции библиотеки из плана:
  - process_batch(articles) -> BatchResult;
  - get_event(event_id) -> EventBundle (B6);
  - export_events() -> снимок каталога.

Обработка:
  1. StateStore определяет, какие статьи уже извлечены (пропускаем, экономя LLM);
  2. для новых/изменённых статей вызывается extract_article (B3);
  3. извлечённые утверждения накапливаются в каталоге событий;
  4. результат и журнал сохраняются.

Ошибка одной статьи не останавливает весь батч. При исчерпании бюджета
оставшиеся статьи помечаются ожидающими.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from news.core.model import Article
from news.llm import budget, client

from llm.events.extract import ExtractionError, extract_article
from llm.events.models import CanonicalEvent, SourceClaim
from llm.events.state import BatchResult, StateStore


class EventCatalog:
    """Простейший каталог событий (B5-минимум): event_id -> CanonicalEvent.

    Здесь же собираются sources/evidence из статей, чтобы EventBundle был полным.
    """

    def __init__(self, catalog_path: str | Path) -> None:
        self.catalog_path = Path(catalog_path)
        self.events: dict[str, CanonicalEvent] = {}
        self._load()

    def _load(self) -> None:
        if self.catalog_path.exists():
            try:
                data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                self.events = {k: CanonicalEvent(**v) for k, v in data.items()}
            except Exception:  # noqa: BLE001
                self.events = {}

    def _save(self) -> None:
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.catalog_path.with_suffix(".tmp.json")
        tmp.write_text(
            json.dumps({k: v.model_dump() for k, v in self.events.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.catalog_path)

    def upsert_from_article(self, article: Article, claims: list[SourceClaim]) -> str:
        """Создаёт или дополняет событие для статьи; возвращает event_id.

        Минимальная группировка B5: одно событие на статью (event_id = article_id).
        Более сложная идентичность событий — отдельный шаг (см. план B5).
        """
        event_id = f"ev:{article.article_id}"
        ev = self.events.get(event_id)
        if ev is None:
            ev = CanonicalEvent(
                event_id=event_id,
                event_version=1,
                title=article.title,
                event_type="other",
                first_published_at=article.published_at,
                updated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                claims=list(claims),
            )
            # sources / evidence из статьи.
            ev.sources = [{
                "source_id": f"src:{article.article_id}:v{len(ev.claims)}",
                "article_id": article.article_id,
                "article_version": 1,
                "title": article.title,
                "publisher": article.source,
                "url": article.url,
                "published_at": article.published_at,
                "content_kind": "original",
                "is_full_original_text": bool(article.text),
                "is_synthetic": False,
            }]
            ev.evidence = [
                {
                    "evidence_id": cid,
                    "source_id": f"src:{article.article_id}:v1",
                    "field": "text",
                    "start": 0,
                    "end": min(200, len(article.text)),
                    "quote": article.text[:200],
                }
                for cid in [c.claim_id for c in claims]
            ]
            self.events[event_id] = ev
        else:
            # Дополняем существующее (обновление версии).
            ev.claims = claims
            ev.event_version += 1
            ev.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._save()
        return event_id

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        ev = self.events.get(event_id)
        return ev.to_bundle() if ev else None

    def export_events(self) -> list[dict[str, Any]]:
        return [ev.to_bundle() for ev in self.events.values()]


class EventsPipeline:
    """Связывает StateStore (B2) и извлечение (B3) с каталогом событий."""

    def __init__(self, state_dir: str | Path, catalog_path: str | Path) -> None:
        self.state = StateStore(state_dir)
        self.catalog = EventCatalog(catalog_path)

    def process_batch(self, articles: list[Article], *, force: bool = False) -> BatchResult:
        result = BatchResult()
        pending = articles if force else self.state.pending_articles(articles)
        result.new_articles = len(pending)
        result.skipped_unchanged = len(articles) - len(pending)

        for article in pending:
            # Проверяем бюджет до каждой статьи.
            if not budget.can_call():
                result.budget_pending.append(article.article_id)
                break
            try:
                claims = extract_article(article)
                result.llm_calls += 1
                event_id = self.catalog.upsert_from_article(article, claims)
                self.state.mark_extracted(article, len(claims))
                if event_id in self.catalog.events:
                    result.events_created += 1
                else:
                    result.events_updated += 1
            except client.BudgetExceeded:
                result.budget_pending.append(article.article_id)
                break
            except ExtractionError as e:
                result.errors.append((article.article_id, str(e)))
                # Не помечаем статью извлечённой, чтобы можно было повторить.
                self.state.journal_append({"kind": "extraction_error", "article_id": article.article_id, "error": str(e)})
            except Exception as e:  # noqa: BLE001
                result.errors.append((article.article_id, str(e)))
                self.state.journal_append({"kind": "error", "article_id": article.article_id, "error": str(e)})

        result.cost_rub = budget.spent_today()
        self.state.journal_append({"kind": "batch", "result": result.to_dict()})
        return result

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        return self.catalog.get_event(event_id)

    def export_events(self) -> list[dict[str, Any]]:
        return self.catalog.export_events()