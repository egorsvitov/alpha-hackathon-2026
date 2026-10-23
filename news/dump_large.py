#!/usr/bin/env python3
"""Крупная выгрузка новостного корпуса с полным текстом.

Предназначена для сбора большого объёма (~10k статей из ~100 источников)
под последующую кластеризацию. Отличия от обычного collect.py:
  - берёт полный текст статей (не headlines-only);
  - устойчива к сбоям сети: заблокированный источник пропускается и
    логируется, но не останавливает весь проход;
  - использует дисковый кэш и продолжает с места остановки (дедуп по URL);
  - очищает накопленный корпус от технического и нецелевого шума;
  - позволяет ограничить цель по числу статей и по времени.

ВАЖНО: полную выгрузку нужно запускать вне корпоративной сети (дома /
хотспот), т.к. на корпоративном Wi-Fi большинство HTTPS-источников
блокируется MITM-прокси.

Запуск из корня:
    .venv_news/bin/python news/dump_large.py
    .venv_news/bin/python news/dump_large.py --target 10000
    .venv_news/bin/python news/dump_large.py --category business --category regional
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=int, default=10000,
                        help="Целевое число статей в корпусе (по умолчанию 10000).")
    parser.add_argument("--max-seconds", type=float, default=0,
                        help="Ограничить время прохода (сек). 0 = без ограничения.")
    parser.add_argument("--category", action="append",
                        choices=["macro", "industry", "business", "official", "regional"],
                        help="Ограничить категорией (можно несколько раз).")
    parser.add_argument("--registry", default="news/sources.json",
                        help="Путь к реестру источников.")
    parser.add_argument("--out-dir", default="data/processed/news_bulk",
                        help="Куда писать JSONL-накопитель.")
    parser.add_argument("--cache-dir", default="data/processed/news_cache",
                        help="Кэш скачанного.")
    parser.add_argument("--limit-per-source", type=int, default=None,
                        help="Лимит записей с одного источника. По умолчанию берётся из реестра.")
    args = parser.parse_args()

    from news.core.fetcher import Fetcher
    from news.core.pipeline import NewsPipeline
    from news.core.quality import clean_article_text, is_noise_article
    from news.core.registry import load_registry
    from news.core.store import JsonlStore

    registry = load_registry(args.registry)
    if args.category:
        sources = registry.enabled(args.category)
    else:
        sources = registry.sources
    # Per-source лимит: если передан явно — переопределяем реестр, иначе берём из реестра.
    if args.limit_per_source is not None:
        for s in sources:
            object.__setattr__(s, "limit", args.limit_per_source)

    store = JsonlStore(args.out_dir)
    fetcher = Fetcher(cache_dir=args.cache_dir, delay=0.3)
    pipe = NewsPipeline(registry, fetcher, store)

    start = time.time()
    fetched_total = 0
    ok_names: list[str] = []
    fail_list: list[tuple[str, str]] = []

    # Читаем и сразу очищаем существующий корпус от ранее накопленного шума.
    raw_existing = store.read_existing()
    merged = {}
    sanitized_changed = False
    for url_key, art in raw_existing.items():
        cleaned_txt = clean_article_text(art.text, title=art.title, url=art.url)
        if is_noise_article(url=art.url, title=art.title, text=cleaned_txt, source=art.source):
            sanitized_changed = True
            continue
        if cleaned_txt != art.text:
            art = art.model_copy(update={"text": cleaned_txt})
            sanitized_changed = True
        merged[url_key] = art
    if sanitized_changed:
        store.write(list(merged.values()))

    for spec in sources:
        if args.target and len(merged) >= args.target:
            print(f"Достигнута цель {args.target} статей, останавливаемся.")
            break
        if args.max_seconds and time.time() - start > args.max_seconds:
            print(f"Время истекло ({args.max_seconds}s), останавливаемся.")
            break
        # Собираем по одному источнику вручную, чтобы учесть таймауты сети.
        try:
            candidates = fetcher.fetch_candidates(spec)
        except Exception as e:  # noqa: BLE001
            fail_list.append((spec.name, str(e)[:60]))
            continue
        if not candidates:
            ok_names.append(f"{spec.name}(0)")
            print(f"  [{spec.name}] +0 (всего в корпусе: {len(merged)})", flush=True)
            continue
        new_count = 0
        for c in candidates:
            existing_art = merged.get(c.url)
            if existing_art is not None and existing_art.text:
                continue
            res = fetcher.fetch_article(c, source=spec.name)
            if res.error:
                continue
            art = pipe._candidate_to_article(
                spec, c, text=res.text, title=res.title, published_at=res.published_at)
            key = art.url or art.article_id
            was = key in merged and bool(merged[key].text)
            merged[key] = art
            new_count += 0 if was else 1
        fetched_total += new_count
        ok_names.append(f"{spec.name}({new_count})")
        # Пишем после каждого источника, если появились новые статьи.
        if new_count > 0:
            store.write(list(merged.values()))
        print(f"  [{spec.name}] +{new_count} (всего в корпусе: {len(merged)})", flush=True)

    # Итоговое сохранение.
    if fetched_total > 0 or sanitized_changed or not store.corpus_path.exists():
        store.write(list(merged.values()))
    fetcher.close()

    print("\n=== ИТОГ ===")
    print(f"Статей в корпусе: {len(merged)}")
    print(f"С полным текстом за этот проход: {fetched_total}")
    print(f"Источников OK ({len(ok_names)}): {', '.join(ok_names[:15])}")
    if fail_list:
        print(f"Источников с ошибкой ({len(fail_list)}):")
        for name, err in fail_list[:15]:
            print(f"  - {name}: {err}")
    print(f"Корпус: {store.corpus_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
