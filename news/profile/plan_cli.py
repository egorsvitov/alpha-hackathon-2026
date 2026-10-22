#!/usr/bin/env python3
"""Генерация тематического плана регулярного батча по аудитории.

Анализирует CSV целевой аудитории (юрлица/платежи) и выводит план новостного
батча: категории источников, темы, приоритетные страны ВЭД, регионы, источники.

Запуск из корня:
    .venv_news/bin/python news/profile/plan_cli.py
    .venv_news/bin/python news/profile/plan_cli.py --uid news/df_Uid_data_hack.csv
    .venv_news/bin/python news/profile/plan_cli.py --json

Данные читаются локально; в git ничего не пушится.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from news.core.registry import load_registry  # noqa: E402
from news.profile.audience import analyze_audience  # noqa: E402
from news.profile.batch_plan import build_batch_plan  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", default="news/df_Uid_data_hack.csv",
                        help="CSV юрлиц (df_Uid_data_hack.csv)")
    parser.add_argument("--fid", default=None, help="CSV связей физлиц (опц.)")
    parser.add_argument("--pay", default="news/df_Uid_payment_hack.csv",
                        help="CSV платежей (df_Uid_payment_hack.csv)")
    parser.add_argument("--registry", default="news/sources.json",
                        help="Реестр источников")
    parser.add_argument("--json", action="store_true", help="Вывести JSON")
    args = parser.parse_args()

    profile = analyze_audience(args.uid, args.fid, args.pay)
    registry = load_registry(args.registry)
    plan = build_batch_plan(profile, registry.sources)

    if args.json:
        print(json.dumps({
            "profile": {
                "total_companies": profile.total_companies,
                "active_clients": profile.active_clients,
                "fts_share": profile.fts_share,
                "top_industries": profile.top_industries[:10],
                "top_trade_countries": profile.top_trade_countries[:10],
            },
            "plan": plan.to_dict(),
        }, ensure_ascii=False, indent=2))
        return 0

    print("=== Профиль целевой аудитории ===")
    print(profile.summary())
    print("\n=== План регулярного батча ===")
    print(f"Категории источников: {', '.join(plan.categories)}")
    print(f"Темы: {', '.join(plan.top_topics) or '(не определены)'}")
    print(f"Приоритетные страны ВЭД: {', '.join(plan.priority_countries)}")
    print(f"Регионы концентрации: {', '.join(plan.top_regions)}")
    if plan.source_names:
        print(f"Источники: {', '.join(plan.source_names)}")
    print("\nОбоснование:")
    for r in plan.reasoning:
        print(f"  - {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())