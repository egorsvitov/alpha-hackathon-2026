#!/usr/bin/env python3
"""Сквозной тест нашего новостного потока → B2 (архив статей).

Показывает, как статьи из news/ (наш парсер) проходят через строгий
ArticleInput и попадают в EventStore (B2). Это подтверждает, что разрыв
«news/ → llm/events/», отмеченный в README, закрывается адаптером.

Запуск (Python 3.13):
    .venv_events/bin/python llm/events/news_adapter_test.py

НЕ вызывает LLM и НЕ тратит бюджет. Только B2: валидация, версии, батч.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from llm.events.storage import EventStore  # noqa: E402
from llm.events.news_adapter import load_news_corpus  # noqa: E402


def main() -> int:
    corpus = Path("data/processed/news/news_corpus.jsonl")
    if not corpus.exists():
        print(f"Нет корпуса: {corpus}. Соберите его: news/collect.py")
        return 2

    articles = load_news_corpus(corpus)
    print(f"Статей с текстом из news/: {len(articles)}")
    if not articles:
        print("Нет статей с текстом — извлечение невозможно.")
        return 3
    print("Пример:", articles[0]["article_id"], "| len text:", len(articles[0]["text"]))

    # Пишем JSON-массив во временный файл для import_batch.
    with tempfile.TemporaryDirectory() as tmp:
        batch_file = Path(tmp) / "corpus.json"
        batch_file.write_text(json.dumps(articles, ensure_ascii=False), encoding="utf-8")
        db = Path(tmp) / "state.sqlite3"

        store = EventStore(db_path=db)
        receipt = store.import_batch(batch_file)

        print("Статус батча:", receipt.import_status)
        print("Всего элементов:", receipt.total_count)

        # Кол-во валидных статей в каталоге.
        with store._transaction() as conn:
            n_new = conn.execute("SELECT COUNT(*) FROM batch_items WHERE disposition='new'").fetchone()[0]
            n_invalid = conn.execute("SELECT COUNT(*) FROM batch_items WHERE disposition='invalid'").fetchone()[0]
        print(f"Новых версий: {n_new}")
        print(f"Невалидных: {n_invalid}")

        if n_invalid:
            print("Примеры невалидных причин — см. issues в батче.")
            return 4

    print("Сквозной тест news→B2 OK: статьи приняты, версии созданы.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())