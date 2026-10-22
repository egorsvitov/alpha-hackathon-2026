#!/usr/bin/env python3
"""CLI поиска и добавления новых источников под целевую аудиторию.

Объединяет профиль аудитории (CSV), discovery-рекомендации и проверку
доступности источников. Показывает, какие темы аудитории не покрыты и какие
потенциальные источники можно добавить; при --add проверяет доступность
рекомендованных источников и добавляет рабочие в sources.json.

Запуск из корня проекта:
    .venv_news/bin/python news/profile/source_discovery_cli.py
    .venv_news/bin/python news/profile/source_discovery_cli.py --add
    .venv_news/bin/python news/profile/source_discovery_cli.py --add --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import ssl
from pathlib import Path

# Корень проекта (два уровня вверх от news/profile/source_discovery_cli.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from news.profile.audience import analyze_audience  # noqa: E402
from news.profile.source_discovery import discover_sources  # noqa: E402
from news.core.registry import load_registry, SourceSpec  # noqa: E402


def _probe(url: str, timeout: int = 6) -> tuple[str, int]:
    """Проверяет доступность ленты. Возвращает (статус, число записей/0)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read()
        import feedparser
        f = feedparser.parse(body)
        return str(r.status), len(f.entries)
    except urllib.error.HTTPError as e:
        return str(e.code), 0
    except Exception:
        return "unreachable", 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid-csv", default="news/df_Uid_data_hack.csv")
    parser.add_argument("--registry", default="news/sources.json")
    parser.add_argument("--add", action="store_true",
                        help="Проверить рекомендованные источники и добавить рабочие в реестр.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Не записывать в реестр, только показать.")
    parser.add_argument("--min-entries", type=int, default=5,
                        help="Мин. число записей, чтобы считать источник рабочим.")
    args = parser.parse_args()

    if not Path(args.uid_csv).exists():
        print(f"Нет CSV профиля: {args.uid_csv}")
        return 1

    profile = analyze_audience(args.uid_csv)
    registry = load_registry(args.registry)
    res = discover_sources(profile, existing_sources=registry.sources)

    print(f"Аудитория: {profile.total_companies} компаний")
    print(f"Темы аудитории: {', '.join(res.current_topics) or '(не определены)'}")
    if res.gaps:
        print(f"Ниши без источников: {', '.join(res.gaps)}")

    if not res.suggestions:
        print("\nВсе темы аудитории уже покрыты текущими источниками.")
        return 0

    print(f"\nРекомендовано новых источников: {len(res.suggestions)}")
    for s in res.suggestions:
        print(f"  {s.name:16} [{s.category:8}] покрывает {','.join(s.topics):20} "
              f"(покрытие {s.coverage} компаний)")

    if not args.add:
        print("\nДобавьте --add, чтобы проверить доступность и внести рабочие в реестр.")
        return 0

    # Проверяем доступность и добавляем рабочие.
    print("\nПроверка доступности…")
    to_add = []
    for s in res.suggestions:
        status, entries = _probe(s.url)
        ok = status == "200" and entries >= args.min_entries
        print(f"  {s.name:16} status={status:6} entries={entries:4} -> "
              f"{'ДОБАВИТЬ' if ok else 'skip'}")
        if ok:
            to_add.append(s)

    if args.dry_run:
        print(f"\n[dry-run] готово к добавлению: {[s.name for s in to_add]}")
        return 0

    # Добавляем в реестр.
    existing_names = {s.name for s in registry.sources}
    new_entries = []
    for s in to_add:
        if s.name in existing_names:
            continue
        new_entries.append({
            "name": s.name, "kind": s.kind, "url": s.url,
            "category": s.category, "limit": 100, "language": "ru",
        })
        existing_names.add(s.name)

    if not new_entries:
        print("Рабочих новых источников нет (все уже есть или недоступны).")
        return 0

    # Пишем в sources.json.
    reg_path = Path(args.registry)
    data = json.loads(reg_path.read_text(encoding="utf-8"))
    data["sources"].extend(new_entries)
    reg_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nДобавлено в {args.registry}: {[e['name'] for e in new_entries]}")
    print("Совет: перепроверьте вручную, что ленты отдают статьи, "
          "и пересоберите корпус (news/dump_large.py).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())