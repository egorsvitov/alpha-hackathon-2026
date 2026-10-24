"""Предрасчёт сохранённых ответов аналитика (офлайн-режим ``cached`` и экономия бюджета).

Запуск из ``backend`` (ключ берётся из окружения или ``backend/.env``)::

    python -m app.analyst.precompute                    # 3 компании × 7 событий × 4 роли
    python -m app.analyst.precompute --dry-run          # только посчитать кандидатов и оценку ₽
    python -m app.analyst.precompute --roles cfo --companies SYNTHETIC_U_1042 --max-rub 2

Результат пишется в ``app/analyst/cache/<company>/…json`` (синтетика, без ключей).
Уже посчитанные ключи пропускаются — повторный запуск бесплатен. Запуск отменяется,
если расход команды по ``/key/info`` выше ``--stop-above`` (по умолчанию 250 ₽).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.analyst.cache import AnalysisCache
from app.analyst.llm_gateway import BudgetLedger, LLMGateway, estimate_tokens, load_model_registry
from app.analyst.schemas import ROLES
from app.analyst.service import AnalystService
from app.analyst.settings import PACKAGE_DIR, AnalystSettings

MSK = timezone(timedelta(hours=3))
DEFAULT_AS_OF = "2026-10-22T12:00:00+03:00"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companies", default="", help="через запятую; по умолчанию все из seed")
    parser.add_argument("--roles", default=",".join(ROLES))
    parser.add_argument("--events", default="", help="через запятую; по умолчанию все seed-события")
    parser.add_argument("--model", default="")
    parser.add_argument("--as-of", default=DEFAULT_AS_OF)
    parser.add_argument("--max-rub", type=float, default=15.0, help="личный лимит процесса, ₽")
    parser.add_argument("--stop-above", type=float, default=250.0, help="не запускать при расходе команды выше, ₽")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from app.services.seed_data import COMPANIES_DATA, EVENTS_DATA
    from app.services.store import store

    base = AnalystSettings.from_env()
    settings = base.with_overrides(
        mode="llm",
        model=args.model or base.model,
        as_of=args.as_of,
        prefer_cache=True,
        bundled_cache_dir=PACKAGE_DIR / "cache",
        runtime_cache_dir=PACKAGE_DIR / "cache",  # пишем прямо в сохранённые ответы репозитория
        max_parallel=3,
    )
    companies = [c for c in COMPANIES_DATA if not args.companies or c["company_id"] in args.companies.split(",")]
    events = [e for e in EVENTS_DATA if not args.events or e["event_id"] in args.events.split(",")]
    roles = [r for r in args.roles.split(",") if r in ROLES]
    total = len(companies) * len(events) * len(roles)
    print(f"Кандидатов: {total} ({len(companies)} компаний × {len(events)} событий × {len(roles)} ролей), модель {settings.model}")

    if args.dry_run or not settings.has_key:
        if not settings.has_key:
            print("LLM_API_KEY не задан — только оценка без запросов")
        svc = AnalystService(settings.with_overrides(mode="cached"))
        tokens = 0
        for comp in companies:
            for role in roles:
                inp = svc.build_input(comp, role, events, store._score_event_for_company)  # noqa: SLF001
                from app.analyst import prompts
                from app.analyst.view import build_prompt_view

                for cand in inp.candidates:
                    tokens += estimate_tokens(prompts.build_analyst_messages(build_prompt_view(inp, cand).data))
        model = load_model_registry().get(settings.model)
        est = model.tariff_cost(tokens, total * 900)
        print(f"Оценка входа {tokens} токенов; по тарифу ≈ {est:.2f} ₽, с запасом ×4 ≈ {est * 4:.2f} ₽")
        return 0

    ledger = BudgetLedger(settings.daily_budget_rub, session_cap_rub=args.max_rub)
    llm = LLMGateway(
        api_key=settings.api_key, base_url=settings.base_url, fallback_base_url=settings.fallback_base_url, ledger=ledger,
        timeout_s=settings.timeout_s, max_retries=settings.max_retries, max_json_repairs=settings.max_json_repairs,
    )
    info = llm.refresh_remote_spend(force=True)
    before = info["spend"] if info else None
    print(f"Расход команды до запуска: {before} ₽")
    if before is not None and before > args.stop_above:
        print("Расход команды выше порога — предрасчёт отменён", file=sys.stderr)
        return 3

    cache = AnalysisCache(runtime_dir=settings.runtime_cache_dir, bundled_dir=None)
    svc = AnalystService(settings, llm=llm, cache=cache, ledger=ledger)
    started = time.perf_counter()
    stats: Counter[str] = Counter()
    for comp in companies:
        for role in roles:
            inp = svc.build_input(comp, role, events, store._score_event_for_company)  # noqa: SLF001
            output, results = svc.analyst.analyze(inp, allow_live=True, prefer_cache=True)
            for cand in inp.candidates:
                res = results[cand.candidate_id]
                if isinstance(res, Exception):
                    stats["failed"] += 1
                    print(f"  ! {comp['company_id']} {role} {cand.candidate_id}: {res}")
                    continue
                kind = "insight" if res.insight else "excluded"
                stats[kind] += 1
                stats["cache_hit" if any(t.step == "cache_lookup" for t in res.run.trace) else "live"] += 1
            line = ", ".join(
                f"{i.event_refs[0].event_id}:{i.decision}:{i.priority_score:.2f}" for i in output.ranked_insights
            )
            print(f"{comp['company_id']} {role}: {line} | исключено {len(output.excluded_candidates)} | "
                  f"{output.run_metadata.cost_rub:.4f} ₽")
    info = llm.refresh_remote_spend(force=True)
    after = info["spend"] if info else None
    print(json.dumps({"stats": stats, "session_cost_rub": round(ledger.session_spent_rub, 4), "team_spend_before": before,
                      "team_spend_after": after, "wall_s": round(time.perf_counter() - started, 1)}, ensure_ascii=False))
    return 0 if not stats["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
