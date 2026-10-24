"""Живая оценка аналитика на пакетах ретривера (8 компаний × 6 кандидатов).

Пакеты НЕ лежат в репозитории (производные данных кейса): путь — ``--handoff-dir`` или
``ANALYST_HANDOFF_DIR``. Запуск из ``backend`` (ключ — из окружения или ``backend/.env``)::

    python -m app.analyst.eval.handoff8_run --dry-run          # только оценка токенов
    python -m app.analyst.eval.handoff8_run --max-rub 12       # живой прогон (кэш включён)

Как в проде: кэш ответов (повторный запуск бесплатен), QA, память фактов (SQLite вне
репозитория; ``--no-memory`` — без неё). Результат без секретов и без текстов профиля —
``app/analyst/eval/handoff8_results.json``; полные ответы для ручного чтения —
``--dump`` (по умолчанию во временной папке ОС, не в репозитории).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.analyst import prompts
from app.analyst.analyst import AnalysisUnavailable, BusinessAnalyst
from app.analyst.batch_adapter import ImpactBatchAdapter
from app.analyst.cache import AnalysisCache
from app.analyst.llm_gateway import BudgetLedger, LLMGateway
from app.analyst.memory import MemoryStore
from app.analyst.settings import PACKAGE_DIR, AnalystSettings

MSK = timezone(timedelta(hours=3))
DEFAULT_HANDOFF = "C:/HAKATON/ALPHA/analyst-handoff-8-companies-20261022"
TMP = Path(tempfile.gettempdir())


def _row(company_id: str, cand, res) -> dict:
    ev = cand.analysis_projection.event_bundles[0].event
    row = {"company_id": company_id, "candidate_id": cand.candidate_id, "event_id": ev.event_id, "title": ev.title}
    if isinstance(res, AnalysisUnavailable) or res is None:
        row.update(decision="failed", reason=str(res)[:200], valid_json=False)
        return row
    run = res.run
    usage = run.usage or {}
    if res.insight is not None:
        ins = res.insight
        reason = (ins.company_relevance.text if ins.company_relevance else ins.headline)
        claims, evs, feats = ins.support.event_claim_ids, ins.support.event_evidence_ids, ins.support.company_feature_ids
        stmts = ins.facts + [s for s in (ins.company_relevance, ins.impact_mechanism, ins.ai_conclusion) if s] + ins.risks + ins.opportunities + ins.next_steps
        row.update(
            decision=ins.decision, reason=reason, significance=ins.significance, priority_score=ins.priority_score,
            materiality=ins.materiality, event_status=ins.event_status, inference_status=ins.support.inference_status,
            statements=len(stmts), statements_with_refs=sum(1 for s in stmts if s.claim_ids or s.feature_ids or s.evidence_ids),
            changes_since_previous=ins.changes_since_previous.model_dump() if ins.changes_since_previous else None,
        )
    else:
        exc = res.excluded
        claims = [c for s in exc.facts for c in s.claim_ids]
        evs = [e for s in exc.facts for e in s.evidence_ids]
        feats = []
        row.update(decision="exclude", reason_code=exc.reason_code, reason=exc.reason)
    row.update(
        ref_counts={"claim_ids": len(claims), "evidence_ids": len(evs), "feature_ids": len(feats)},
        ref_ids={"claim_ids": claims, "evidence_ids": evs, "feature_ids": feats},
        qa_status=run.qa_status, qa_codes=sorted({i.code for i in run.qa_issues if i.severity != "info"}),
        generated_by=run.generated_by, model=run.model, prompt_version=run.prompt_version,
        prompt_tokens=usage.get("prompt_tokens", 0), completion_tokens=usage.get("completion_tokens", 0),
        cost_rub=round(float(usage.get("cost_rub", 0.0)), 5), latency_ms=usage.get("latency_ms", 0),
        llm_calls=usage.get("calls", 0), json_repairs=usage.get("json_repairs", 0),
        valid_json=run.generated_by == "rules" or bool(usage.get("calls")),
        valid_json_first_try=run.generated_by == "rules" or (bool(usage.get("calls")) and usage.get("json_repairs", 0) == 0),
    )
    return row


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    dec = Counter(r["decision"] for r in rows)
    llm_rows = [r for r in rows if r.get("generated_by") != "rules" and r["decision"] != "failed"]
    stm = sum(r.get("statements", 0) for r in rows)
    stm_ref = sum(r.get("statements_with_refs", 0) for r in rows)
    qa_codes = Counter(c for r in rows for c in r.get("qa_codes", []))
    cost = sum(r.get("cost_rub", 0.0) for r in rows)
    return {
        "candidates": n,
        "decisions": dict(dec),
        "share": {k: round(v / n, 3) for k, v in dec.items()} if n else {},
        "analyzed_by_llm": len(llm_rows),
        "excluded_by_rules": sum(1 for r in rows if r.get("generated_by") == "rules"),
        "valid_json_share": round(sum(1 for r in llm_rows if r.get("valid_json")) / len(llm_rows), 3) if llm_rows else None,
        "valid_json_first_try_share": round(sum(1 for r in llm_rows if r.get("valid_json_first_try")) / len(llm_rows), 3) if llm_rows else None,
        "statement_ref_coverage": round(stm_ref / stm, 3) if stm else None,
        "insights_with_feature_refs": sum(1 for r in rows if r["decision"] in ("include", "watch") and r["ref_counts"]["feature_ids"]),
        "qa_rules_triggered": dict(qa_codes.most_common()),
        "qa_status": dict(Counter(r.get("qa_status") for r in rows if r.get("qa_status"))),
        "changes_since_previous": dict(Counter((r.get("changes_since_previous") or {}).get("status") for r in rows if r["decision"] in ("include", "watch"))),
        "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in rows),
        "completion_tokens": sum(r.get("completion_tokens", 0) for r in rows),
        "cost_rub_usage": round(cost, 4),
        "cost_rub_per_llm_candidate": round(cost / len(llm_rows), 4) if llm_rows else None,
        "avg_latency_s": round(sum(r.get("latency_ms", 0) for r in llm_rows) / len(llm_rows) / 1000, 1) if llm_rows else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--handoff-dir", default=os.environ.get("ANALYST_HANDOFF_DIR", DEFAULT_HANDOFF))
    ap.add_argument("--role", default="cfo")
    ap.add_argument("--prompt-version", default=prompts.PROMPT_VERSION_V12)
    ap.add_argument("--model", default="")
    ap.add_argument("--cache-dir", default=str(TMP / "alfa_analyst_cache_handoff8"))
    ap.add_argument("--memory-path", default=str(TMP / "alfa_analyst_memory_handoff8.sqlite3"))
    ap.add_argument("--no-memory", action="store_true")
    ap.add_argument("--fewshot", action="store_true", help="с учебными примерами (ANALYST_FEWSHOT)")
    ap.add_argument("--companies", default="", help="через запятую; по умолчанию все")
    ap.add_argument("--limit-per-company", type=int, default=0, help="не больше N кандидатов на компанию (0 — все)")
    ap.add_argument("--max-rub", type=float, default=12.0, help="личный лимит процесса, ₽")
    ap.add_argument("--stop-above", type=float, default=250.0, help="не запускать при расходе команды выше, ₽")
    ap.add_argument("--out", default=str(PACKAGE_DIR / "eval" / "handoff8_results.json"))
    ap.add_argument("--dump", default=str(TMP / "alfa_handoff8_outputs.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    handoff = Path(args.handoff_dir)
    files = sorted((handoff / "analyst-inputs").glob("*.json"))
    if args.companies:
        files = [p for p in files if p.stem in args.companies.split(",")]
    if not files:
        print(f"Нет пакетов в {handoff}/analyst-inputs", file=sys.stderr)
        return 2
    schema_dir = handoff / "schemas"
    memory = None if args.no_memory else MemoryStore(args.memory_path)
    adapter = ImpactBatchAdapter(prompt_version=args.prompt_version, reserve_tokens=0 if memory is None else 600,
                                 schema_dir=schema_dir if schema_dir.is_dir() else None, fewshot=args.fewshot)
    inputs = [(p.stem, adapter.to_analyst_input(json.loads(p.read_text(encoding="utf-8")), role=args.role)) for p in files]
    est = [t for _, r in inputs for t in r.token_estimates.values()]
    print(f"Пакетов {len(inputs)}, кандидатов {len(est)}; оценка промпта (байты/4): мин {min(est)}, макс {max(est)}, сумма {sum(est)}")
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
                              max_parallel=3, fallback_models=fallbacks, prompt_version=args.prompt_version, memory=memory,
                              fewshot=args.fewshot)
    rows, dumps = [], []
    started = time.perf_counter()
    for company_id, adapted in inputs:
        inp = adapted.analyst_input
        if args.limit_per_company:
            inp = inp.model_copy(update={"candidates": inp.candidates[: args.limit_per_company]})
        output, results = analyst.analyze(inp, allow_live=True, prefer_cache=True)
        for cand in inp.candidates:
            rows.append(_row(company_id, cand, results.get(cand.candidate_id)))
        dumps.append(output.model_dump(mode="json"))
        print(f"{company_id}: " + ", ".join(f"{r['decision'][:3]}" for r in rows[-len(inp.candidates):])
              + f" | {output.run_metadata.cost_rub:.4f} ₽ (usage)")
    info = llm.refresh_remote_spend(force=True)
    after = info["spend"] if info else None
    result = {
        "generated_at": datetime.now(MSK).isoformat(timespec="seconds"),
        "role": args.role, "model": model, "prompt_version": analyst.prompt_version, "memory": memory is not None,
        "source": "ImpactCandidateBatch v1, 8 компаний × 6 кандидатов (пакеты ретривера вне репозитория)",
        "summary": summarize(rows),
        "run_cost": {"session_spent_rub": round(ledger.session_spent_rub, 4), "team_spend_before_rub": before,
                     "team_spend_after_rub": after, "wall_s": round(time.perf_counter() - started, 1)},
        "candidates": rows,
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    Path(args.dump).write_text(json.dumps(dumps, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"summary": result["summary"], "run_cost": result["run_cost"]}, ensure_ascii=False, indent=1))
    return 0 if not any(r["decision"] == "failed" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
