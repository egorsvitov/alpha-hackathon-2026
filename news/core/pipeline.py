"""Пайплайн сбора: источники → кандидаты → текст → статьи → JSONL.

Это оркестратор пункта 2. Он:
  1. читает реестр источников (news/sources.json);
  2. для каждого источника собирает кандидатов (ссылки);
  3. для каждого кандидата скачивает и извлекает текст;
  4. собирает Article в формате команды и пишет в JSONL-накопитель.

Обработка ошибок изолирована: сбой одного источника или одной статьи
не останавливает весь проход. Модуль не трогает сеть кроме fetcher.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from news.core.fetcher import Fetcher
from news.core.identity import normalize_time, stable_id
from news.core.model import Article
from news.core.registry import load_registry, SourceRegistry
from news.core.store import JsonlStore


@dataclass
class PipelineReport:
    """Итоги одного прохода для лога и отладки."""

    articles_written: int = 0
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[tuple[str, str]] = field(default_factory=list)
    fetched: int = 0
    failed_articles: int = 0


class NewsPipeline:
    def __init__(
        self,
        registry: SourceRegistry,
        fetcher: Fetcher,
        store: JsonlStore,
    ) -> None:
        self.registry = registry
        self.fetcher = fetcher
        self.store = store

    def run(self, categories: list[str] | None = None, headlines_only: bool = False) -> PipelineReport:
        report = PipelineReport()
        sources = self.registry.enabled(categories)
        new_articles: list[Article] = []

        for spec in sources:
            try:
                candidates = self.fetcher.fetch_candidates(spec)
                report.sources_ok.append(spec.name)
            except Exception as e:  # noqa: BLE001
                report.sources_failed.append((spec.name, str(e)))
                continue

            # Без полного текста берём только заголовок/ссылку.
            if headlines_only or spec.headlines_only:
                for c in candidates:
                    new_articles.append(self._candidate_to_article(spec, c, text=""))
                continue

            for c in candidates:
                res = self.fetcher.fetch_article(c)
                if res.error:
                    report.failed_articles += 1
                    # Всё равно сохраняем заголовок/ссылку без текста, чтобы
                    # не терять факт существования статьи.
                    new_articles.append(self._candidate_to_article(spec, c, text=""))
                    continue
                report.fetched += 1
                new_articles.append(
                    self._candidate_to_article(
                        spec, c, text=res.text, title=res.title, published_at=res.published_at
                    )
                )

        # Объединяем с существующим корпусом (дедупликация по URL) и пишем.
        merged = self.store.merge(new_articles)
        self.store.write(list(merged.values()))
        report.articles_written = len(merged)
        return report

    @staticmethod
    def _candidate_to_article(
        spec,
        candidate,
        text: str = "",
        title: str | None = None,
        published_at: str | None = None,
    ) -> Article:
        return Article(
            article_id=stable_id(spec.name, candidate.url),
            source=spec.name,
            url=candidate.url,
            title=(title or candidate.title),
            published_at=normalize_time(published_at or candidate.published_at),
            language=spec.language,
            text=text,
        )


def build_pipeline(
    registry_path: str | Path = "news/sources.json",
    out_dir: str | Path = "data/processed/news",
    cache_dir: str | Path = "data/processed/news_cache",
) -> NewsPipeline:
    """Собирает пайплайн со стандартными путями из корня проекта."""
    from news.core.fetcher import Fetcher

    registry = load_registry(registry_path)
    fetcher = Fetcher(cache_dir=cache_dir)
    store = JsonlStore(out_dir)
    return NewsPipeline(registry, fetcher, store)