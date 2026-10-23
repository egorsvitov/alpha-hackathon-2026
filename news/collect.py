#!/usr/bin/env python3
"""Универсальный сборщик новостей (пункт 2).

Собирает статьи из реестра источников (news/sources.json), извлекает текст
и пишет JSONL-накопитель в data/processed/news/news_corpus.jsonl в формате:

    {
      "article_id": "source:hash",
      "source": "Название СМИ",
      "url": "https://...",
      "title": "Заголовок",
      "published_at": "2026-10-20T12:00:00+03:00",
      "language": "ru",
      "text": "Текст для обработки NLP-модулем..."
    }

Примеры:
    python3 news/collect.py                       # все источники
    python3 news/collect.py --category macro      # только макро
    python3 news/collect.py --category regional   # только региональные СМИ
    python3 news/collect.py --headlines-only      # без полного текста
    python3 news/collect.py --dry-run             # только кандидаты, без сети к статьям

Требует Python 3.10+ и зависимости из news/requirements.txt.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Позволяем запуск из корня проекта (news/collect.py) и из папки news/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--category", action="append",
        choices=["macro", "industry", "business", "official", "regional"],
        help="Ограничить категорией источников (можно несколько раз).",
    )
    parser.add_argument("--headlines-only", action="store_true",
                        help="Собирать только заголовки/ссылки без полного текста.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Показать, сколько кандидатов нашлось, без скачивания статей.")
    parser.add_argument("--registry", default="news/sources.json", help="Путь к реестру источников.")
    parser.add_argument("--out-dir", default="data/processed/news", help="Куда писать JSONL.")
    parser.add_argument("--cache-dir", default="data/processed/news_cache", help="Кэш скачанного.")
    parser.add_argument("--audience-csv", default=None,
                        help="CSV юрлиц целевой аудитории (df_Uid_data_hack.csv). Если задан, "
                             "строит тематический план батча и фильтрует новости под аудиторию.")
    args = parser.parse_args()

    try:
        from news.core.pipeline import build_pipeline
        pipeline = build_pipeline(registry_path=args.registry, out_dir=args.out_dir, cache_dir=args.cache_dir)
    except Exception as e:  # noqa: BLE001
        print(f"Ошибка инициализации: {e}", file=sys.stderr)
        return 2

    # Если задан CSV аудитории — строим тематический фильтр и применяем к сбору.
    topic_filter = None
    if args.audience_csv:
        try:
            from news.profile.audience import analyze_audience
            from news.profile.batch_plan import build_batch_plan
            from news.profile.topic_filter import TopicFilter
            profile = analyze_audience(args.audience_csv, None, None)
            plan = build_batch_plan(profile, pipeline.registry.sources)
            topic_filter = TopicFilter(plan)
            print(f"[audience] темы: {', '.join(plan.top_topics)}")
            print(f"[audience] регионы: {', '.join(plan.top_regions)}")
            print(f"[audience] страны ВЭД: {', '.join(plan.priority_countries[:6])}")
        except Exception as e:  # noqa: BLE001
            print(f"Предупреждение: не удалось построить фильтр аудитории: {e}", file=sys.stderr)

    try:
        report = pipeline.run(categories=args.category, headlines_only=args.headlines_only,
                              topic_filter=topic_filter)
    except Exception as e:  # noqa: BLE001
        print(f"Ошибка сбора: {e}", file=sys.stderr)
        return 3

    print(f"Корпус записан в: {pipeline.store.corpus_path}")
    print(f"Всего статей в корпусе: {report.articles_written}")
    print(f"Статей с текстом за проход: {report.fetched}")
    print(f"Статей без текста: {report.failed_articles}")
    print(f"Источников OK ({len(report.sources_ok)}): {', '.join(report.sources_ok)}")
    if report.sources_failed:
        print("Источников с ошибкой:")
        for name, err in report.sources_failed:
            print(f"  - {name}: {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
