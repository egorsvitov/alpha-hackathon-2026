#!/usr/bin/env python3
"""Smoke-тест пайплайна без сети: фейковый fetcher → JSONL.

Проверяет, что реестр валиден, кандидаты превращаются в Article,
дедупликация по URL работает и файл пишется в формате команды.
Запуск из корня проекта:  python3 news/smoke_test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news.core.fetcher import Fetcher  # noqa: E402
from news.core.model import ArticleCandidate  # noqa: E402
from news.core.registry import SourceSpec, load_registry  # noqa: E402
from news.core.store import JsonlStore  # noqa: E402
from news.core.pipeline import NewsPipeline  # noqa: E402


class FakeFetcher:
    def fetch_candidates(self, spec: SourceSpec):
        return [
            ArticleCandidate(url="https://example.com/a", title="Статья A"),
            ArticleCandidate(url="https://example.com/b", title="Статья B"),
            ArticleCandidate(url="https://example.com/a", title="Статья A (дубль)"),
        ]

    def fetch_article(self, candidate: ArticleCandidate):
        return type("R", (), {"text": "Текст " + candidate.title, "title": candidate.title,
                              "published_at": None, "error": None})()


def main() -> int:
    registry = load_registry("news/sources.json")
    if not registry.sources:
        print("FAIL: реестр пуст")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        store = JsonlStore(tmp)
        pipe = NewsPipeline(registry, FakeFetcher(), store)
        # Запускаем только первый источник (cbr_rss).
        first = registry.sources[0]
        spec_reg = type("R", (), {"enabled": lambda self, c=None: [first], "sources": []})()

        # Вручную: эмулируем один проход через run с подменой реестра.
        pipe.registry = spec_reg
        report = pipe.run()
        assert report.articles_written == 2, f"ожидали дедуп до 2, получили {report.articles_written}"

        lines = store.corpus_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2, f"в файле должно быть 2 строки, есть {len(lines)}"
        art = json.loads(lines[0])
        for field in ("article_id", "source", "url", "title", "published_at", "language", "text"):
            assert field in art, f"нет поля {field}"
        assert art["text"].startswith("Текст"), "текст не извлечён"
        assert art["source"] == first.name

    print("smoke test OK: реестр валиден, дедуп работает, формат статьи корректен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())