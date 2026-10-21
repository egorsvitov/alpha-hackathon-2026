#!/usr/bin/env python3
"""CLI для B2+B3: обработка батча статей → каталог событий.

Читает статьи из JSONL-накопителя news/ (data/processed/news/news_corpus.jsonl)
и прогоняет их через process_batch. Выход — каталог событий и отчёт.

Запуск из корня:
    .venv_news/bin/python llm/events/legacy/run.py
    .venv_news/bin/python llm/events/legacy/run.py --limit 20
    .venv_news/bin/python llm/events/legacy/run.py --dry-run

ВНИМАНИЕ: извлечение вызывает LLM (тратит бюджет). На корпоративной сети
шлюз заблокирован — реальные вызовы работают вне прокси.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from news.core.model import Article  # noqa: E402
from llm.events.legacy.pipeline import EventsPipeline  # noqa: E402


def load_articles(jsonl_path: str | Path, limit: int | None = None) -> list[Article]:
    p = Path(jsonl_path)
    if not p.exists():
        print(f"Файл корпуса не найден: {p}", file=sys.stderr)
        return []
    out: list[Article] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(Article.model_validate_json(line))
        except Exception:  # noqa: BLE001
            continue
        if limit and len(out) >= limit:
            break
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/processed/news/news_corpus.jsonl",
                        help="JSONL-накопитель статей")
    parser.add_argument("--state-dir", default="data/processed/events_state",
                        help="Директория состояния B2")
    parser.add_argument("--catalog", default="data/processed/events_catalog.json",
                        help="Файл каталога событий")
    parser.add_argument("--limit", type=int, default=None, help="Обработать не более N статей")
    parser.add_argument("--force", action="store_true", help="Переизвлечь даже неизменённые статьи")
    parser.add_argument("--dry-run", action="store_true",
                        help="Показать, сколько статей к обработке, без вызова LLM")
    args = parser.parse_args()

    articles = load_articles(args.input, args.limit)
    print(f"Статей загружено: {len(articles)}")

    pipe = EventsPipeline(args.state_dir, args.catalog)
    pending = pipe.state.pending_articles(articles)
    print(f"К обработке (новых/изменённых): {len(pending)}")
    if args.dry_run:
        print("dry-run: вызов LLM не выполняется.")
        return 0

    result = pipe.process_batch(articles, force=args.force)
    print("Отчёт:")
    for k, v in result.to_dict().items():
        print(f"  {k}: {v}")

    out_path = args.catalog
    print(f"Каталог событий: {out_path} (событий: {len(pipe.export_events())})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
