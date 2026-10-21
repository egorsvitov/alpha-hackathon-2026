#!/usr/bin/env python3
"""Офлайн-проверка B2+B3 без сети и LLM.

Проверяет:
  - StateStore: дедупликация (повторный батч пропускает уже извлечённое);
  - BatchResult: подсчёт new/skipped;
  - EventCatalog: создание события, get_event/export_events.

Извлечение (extract_article) здесь подменяется фейковой функцией, чтобы не
тратить бюджет и не зависеть от сети. Реальный вызов LLM — в extract.py.

Запуск:  .venv_news/bin/python llm/events/legacy/smoke_test.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from news.core.model import Article  # noqa: E402

import llm.events.legacy.pipeline as pipeline_mod  # noqa: E402
from llm.events.legacy.pipeline import EventsPipeline  # noqa: E402
from llm.events.legacy.models import SourceClaim  # noqa: E402


def fake_extract(article: Article):
    """Фейковое извлечение одного утверждения (без LLM)."""
    return [
        SourceClaim(
            claim_id=f"{article.article_id}:claim-1",
            text=f"Извлечено из {article.title}",
            modality="asserted",
            polarity="affirmed",
        )
    ]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "state"
        catalog_path = Path(tmp) / "events.json"

        pipe = EventsPipeline(state_dir, catalog_path)
        # Подменяем извлечение фейком (в модуле pipeline, куда он импортирован).
        pipeline_mod.extract_article = fake_extract

        arts = [
            Article(article_id="a1", source="test", url="http://x/a1", title="Статья A", text="текст A"),
            Article(article_id="a2", source="test", url="http://x/a2", title="Статья B", text="текст B"),
        ]

        # Первый батч: обе новые.
        r1 = pipe.process_batch(arts)
        assert r1.new_articles == 2, f"ожидали 2 новых, получили {r1.new_articles}"
        assert r1.skipped_unchanged == 0, "первый проход ничего не пропускает"
        assert len(pipe.export_events()) == 2, "должно быть 2 события"

        # Второй батч с теми же статьями: всё пропущено (дедуп).
        r2 = pipe.process_batch(arts)
        assert r2.new_articles == 0, f"повторный батч не должен извлекать, получено {r2.new_articles}"
        assert r2.skipped_unchanged == 2, f"ожидали 2 пропущенных, получено {r2.skipped_unchanged}"

        # Изменённый текст статьи a1 = новая версия, извлекается заново.
        a1_changed = Article(article_id="a1", source="test", url="http://x/a1", title="Статья A (обновлено)", text="другой текст")
        r3 = pipe.process_batch([a1_changed])
        assert r3.new_articles == 1, f"изменённая статья должна переизвлечься, получено {r3.new_articles}"

        # get_event по одному событию.
        ev = pipe.get_event("ev:a1")
        assert ev is not None, "событие ev:a1 должно существовать"
        assert ev["event"]["event_id"] == "ev:a1"
        assert ev["schema_version"] == "0.1"
        assert len(ev["claims"]) >= 1, "должно быть хотя бы одно утверждение"

    print("B2+B3 smoke test OK: дедуп, повторный запуск, каталог, get_event работают")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
