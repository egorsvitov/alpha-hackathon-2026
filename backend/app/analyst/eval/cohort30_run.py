"""Живая оценка аналитика на реальной выдаче нового ретривера (cohort30: 30 компаний × CEO/CFO × 20).

Выборка НЕ лежит в репозитории (производна от данных кейса): путь — ``--cohort-dir`` или
``ANALYST_COHORT30_DIR``. Входы берутся из ``analyst-inputs.jsonl`` — готовые ``AnalystInput``,
собранные эталонным адаптером Егора (``adapt_retrieval_batch``), без повторной адаптации.
Запуск из ``backend`` (ключ — из окружения или ``backend/.env``)::

    python -m app.analyst.eval.cohort30_run --companies U_1,U_2 --dry-run   # оценка токенов и ₽
    python -m app.analyst.eval.cohort30_run --companies U_1,U_2 --max-rub 25

Кэш ответов и память фактов — во временной папке ОС (повторный запуск бесплатен). Результат —
``app/analyst/eval/cohort30_results.json``: без секретов, из текстов новостей только заголовки.
Полные ответы для ручного чтения — ``--dump`` (временная папка ОС, не репозиторий).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.analyst import prompts
from app.analyst.analyst import BusinessAnalyst
from app.analyst.cache import AnalysisCache
from app.analyst.eval.handoff8_run import MSK, _row, summarize
from app.analyst.llm_gateway import BudgetLedger, LLMGateway
from app.analyst.memory import MemoryStore
from app.analyst.prompt_budget import estimate_prompt_tokens
from app.analyst.schemas import AnalystInput
from app.analyst.settings import PACKAGE_DIR, AnalystSettings

DEFAULT_COHORT = "C:/HAKATON/ALPHA/Retriver_output/analyst-input-cohort30-20261024"
TMP = Path(tempfile.gettempdir())
# Тариф GLM-5.3-Flash из чата организаторов (₽ за 1 млн токенов) и наблюдаемый множитель списания шлюза.
TARIFF_IN, TARIFF_OUT, GATEWAY_MULT = 5.10, 17.0, (1.0, 3.5)
EXPECTED_COMPLETION = 1500  # токенов ответа на кандидата (с рассуждениями), по прошлым прогонам


def processing_status(cand) -> str:
    return getattr(cand.analysis_projection.event_bundles[0].event, "processing_status", None) or "unknown"


def load_inputs(cohort: Path, role: str, companies: list[str]) -> list[AnalystInput]:
    out = []
    with open(cohort / "analyst-inputs.jsonl", encoding="utf-8") as stream:
        for line in stream:
            inp = AnalystInput.model_validate_json(line)
            if inp.request.role == role and (not companies or inp.request.company_id in companies):
                out.append(inp)
    if companies:  # порядок как в --companies
        out.sort(key=lambda i: companies.index(i.request.company_id))
    return out


def by_state(rows: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for state in sorted({r["processing_status"] for r in rows}):
        part = [r for r in rows if r["processing_status"] == state]
        out[state] = {"candidates": len(part), "decisions": dict(Counter(r["decision"] for r in part))}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cohort-dir", default=os.environ.get("ANALYST_COHORT30_DIR", DEFAULT_COHORT))
    ap.add_argument("--role", default="cfo", choices=["ceo", "cfo"])
    ap.add_argument("--prompt-version", default=prompts.PROMPT_VERSION_V13)
    ap.add_argument("--model", default="")
    ap.add_argument("--cache-dir", default=str(TMP / "alfa_analyst_cache_cohort30"))
    ap.add_argument("--memory-path", default=str(TMP / "alfa_analyst_memory_cohort30.sqlite3"))
    ap.add_argument("--no-memory", action="store_true")
    ap.add_argument("--companies", default="", help="через запятую; по умолчанию все компании выборки")
    ap.add_argument("--limit-per-company", type=int, default=0, help="не больше N кандидатов на компанию (0 — все)")
    ap.add_argument("--max-rub", type=float, default=25.0, help="личный лимит процесса, ₽")
    ap.add_argument("--stop-above", type=float, default=250.0, help="не запускать при расходе команды выше, ₽")
    ap.add_argument("--out", default=str(PACKAGE_DIR / "eval" / "cohort30_results.json"))
    ap.add_argument("--dump", default=str(TMP / "alfa_cohort30_outputs.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    cohort = Path(args.cohort_dir)
    if not (cohort / "analyst-inputs.jsonl").is_file():
        print(f"Нет analyst-inputs.jsonl в {cohort}", file=sys.stderr)
        return 2
    companies = [c for c in args.companies.split(",") if c]
    inputs = load_inputs(cohort, args.role, companies)
    if not inputs:
        print("Нет входов для выбранных компаний и роли", file=sys.stderr)
        return 2
    if args.limit_per_company:
        inputs = [i.model_copy(update={"candidates": i.candidates[: args.limit_per_company]}) for i in inputs]
    memory = None if args.no_memory else MemoryStore(args.memory_path)
    est = [estimate_prompt_tokens(i, c, args.prompt_version) + (0 if memory is None else 600)
           for i in inputs for c in i.candidates]
    states = Counter(processing_status(c) for i in inputs for c in i.candidates)
    n = len(est)
    lo = (sum(est) * TARIFF_IN + n * EXPECTED_COMPLETION * TARIFF_OUT) / 1e6
    print(f"Входов {len(inputs)} ({args.role}, {args.prompt_version}), кандидатов {n} {dict(states)}; "
          f"промпт (байты/4): мин {min(est)}, макс {max(est)}, сумма {sum(est)}; "
          f"оценка ₽: {lo * GATEWAY_MULT[0]:.1f}–{lo * GATEWAY_MULT[1]:.1f} (без кэша)")
    if args.dry_run:
        return 0

    s = AnalystSettings.from_env()
    if not s.has_key:
        print("LLM_API_KEY не задан", file=sys.stderr)
        return 2
    model = args.model or s.model
    ledger = BudgetLedger(s.daily_budget_rub, session_cap_rub=args.max_rub)
    llm = LLMGateway(api_key=s.api_key, base_url=s.base_url, fallback_base_url=s.fallback_base_url, ledger=ledger,
                     timeout_s=s.timeout_s, max_retries=s.max_retries, max_json_repairs=s.max_json_repairs)
    info = llm.refresh_remote_spend(force=True)
    before = info["spend"] if info else None
    print(f"Расход команды до запуска: {before} ₽")
    if before is not None and before > args.stop_above:
        print("Расход команды выше порога — прогон отменён", file=sys.stderr)
        return 3
    fallbacks = [m for m in [s.fallback_model] if m and m != model]
    analyst = BusinessAnalyst(llm, model=model, cache=AnalysisCache(Path(args.cache_dir), None), max_tokens=s.max_tokens,
                              max_parallel=3, fallback_models=fallbacks, prompt_version=args.prompt_version, memory=memory)
    rows, dumps = [], []
    started = time.perf_counter()
    for inp in inputs:
        company_id = inp.request.company_id
        t0 = time.perf_counter()
        output, results = analyst.analyze(inp, allow_live=True, prefer_cache=True)
        for rank, cand in enumerate(inp.candidates, start=1):
            res = results.get(cand.candidate_id)
            row = _row(company_id, cand, res)
            row.update(rank=rank, processing_status=processing_status(cand))
            status = getattr(getattr(res, "excluded", None), "status", None)
            if row["decision"] == "exclude" and status in ("deferred", "failed"):
                row["decision"] = status  # отложен/сбой анализа — не «не важно»
            rows.append(row)
        dumps.append(output.model_dump(mode="json"))
        print(f"{company_id}: " + " ".join(r["decision"][:3] for r in rows[-len(inp.candidates):])
              + f" | {output.run_metadata.cost_rub:.3f} ₽, {time.perf_counter() - t0:.0f} с")
    info = llm.refresh_remote_spend(force=True)
    after = info["spend"] if info else None
    wall = time.perf_counter() - started
    summary = summarize(rows)
    summary["by_processing_status"] = by_state(rows)
    summary["wall_s_per_candidate"] = round(wall / len(rows), 2) if rows else None
    result = {
        "generated_at": datetime.now(MSK).isoformat(timespec="seconds"),
        "role": args.role, "model": model, "prompt_version": analyst.prompt_version, "memory": memory is not None,
        "source": "RetrievalCandidateBatch.v1 → adapt_retrieval_batch (cohort30, вне репозитория)",
        "companies": [i.request.company_id for i in inputs],
        "summary": summary,
        "run_cost": {"session_spent_rub": round(ledger.session_spent_rub, 4), "team_spend_before_rub": before,
                     "team_spend_after_rub": after,
                     "team_spend_delta_rub": round(after - before, 4) if before is not None and after is not None else None,
                     "wall_s": round(wall, 1)},
        "candidates": rows,
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    Path(args.dump).write_text(json.dumps(dumps, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"summary": summary, "run_cost": result["run_cost"]}, ensure_ascii=False, indent=1))
    return 0 if not any(r["decision"] == "failed" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
