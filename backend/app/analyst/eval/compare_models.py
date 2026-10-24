"""Сравнение моделей на представительных кейсах (живые запросы через шлюз).

Запуск из ``backend``::

    python -m app.analyst.eval.compare_models --models z-ai/glm-5.3-flash,deepseek/deepseek-v4.1-flash \
        --max-rub 10 --out app/analyst/eval/results.json

Скрипт не запустится, если расход команды по ``/key/info`` выше ``--stop-above`` (по умолчанию 250 ₽);
личный лимит процесса — ``--max-rub``. В результаты пишутся только метрики и короткие
заголовки — без ключей и без текстов промптов.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.analyst import prompts
from app.analyst.eval.cases import EvalCase, company, eval_cases
from app.analyst.input_adapter import bank_fragments_from_evidence, build_analyst_input
from app.analyst.llm_gateway import BudgetLedger, ChatRequest, JsonRepairFailed, LLMError, LLMGateway
from app.analyst.qa import validate_draft
from app.analyst.schemas import LLMDraft
from app.analyst.settings import AnalystSettings
from app.analyst.view import build_prompt_view

MSK = timezone(timedelta(hours=3))
AS_OF = "2026-10-22T12:00:00+03:00"


def _cyrillic_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    if not letters:
        return 0.0
    return sum(1 for ch in letters if re.match(r"[А-Яа-яЁё]", ch)) / len(letters)


def raw_statement_count(draft: LLMDraft) -> int:
    if draft.decision == "exclude":
        return len(draft.what_happened)
    n = len(draft.what_happened) + len(draft.risks) + len(draft.opportunities) + len(draft.next_steps)
    n += sum(1 for s in (draft.company_relevance, draft.impact_mechanism, draft.ai_conclusion) if s)
    return n


def run_case(llm: Any, model: str, case: EvalCase, *, max_tokens: int = 1600, dump_dir: Path | None = None) -> dict[str, Any]:
    from app.services.store import store

    comp = company(case.company_id)
    as_of = datetime.fromisoformat(AS_OF)
    from app.services.seed_data import SHARED_EVIDENCE

    inp = build_analyst_input(
        comp, case.role, [case.event], as_of=as_of,
        relevance_by_event={case.event["event_id"]: store._score_event_for_company(case.event, comp, case.role)},  # noqa: SLF001
        bank_corpus=bank_fragments_from_evidence(SHARED_EVIDENCE),
    )
    cand = inp.candidates[0]
    view = build_prompt_view(inp, cand)
    request = ChatRequest(messages=prompts.build_analyst_messages(view.data), model=model, max_tokens=max_tokens, purpose="eval")
    row: dict[str, Any] = {"case_id": case.case_id, "model": model}
    started = time.perf_counter()
    try:
        draft, usage = llm.chat_json(request, LLMDraft.model_validate)
    except JsonRepairFailed as exc:
        u = exc.usage.as_dict() if exc.usage else {}
        row.update({"json_valid": False, "json_valid_first_try": False, "error": "invalid_json", "usage": u,
                    "wall_s": round(time.perf_counter() - started, 2)})
        return row
    except LLMError as exc:
        row.update({"json_valid": False, "json_valid_first_try": False, "error": exc.code, "usage": {},
                    "wall_s": round(time.perf_counter() - started, 2)})
        return row
    qa = validate_draft(draft, view, case.role, as_of=inp.request.as_of)
    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        (dump_dir / f"{model.replace('/', '_')}__{case.case_id}.json").write_text(
            json.dumps({"draft": draft.model_dump(), "qa_issues": [i.model_dump() for i in qa.issues]}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    raw = raw_statement_count(draft)
    texts = " ".join(
        [draft.headline]
        + [s.text for s in draft.what_happened + draft.risks + draft.opportunities + draft.next_steps]
        + [s.text for s in (draft.company_relevance, draft.impact_mechanism, draft.ai_conclusion) if s]
    )
    issues = [i.code for i in qa.issues]
    fabrication = [c for c in issues if c in case.forbidden_issue_codes]
    bank_found = qa.bank_assessment.status in ("found", "outdated")
    row.update(
        {
            "json_valid": True,
            "json_valid_first_try": usage.json_repairs == 0,
            "decision_model": draft.decision,
            "decision_final": qa.decision,
            "decision_ok": qa.decision in case.expected_decisions,
            "materiality": draft.materiality,
            "event_status": qa.event_status,
            "event_status_ok": (qa.event_status in case.expect_event_status) if case.expect_event_status else None,
            "bank_status": qa.bank_assessment.status,
            "bank_ok": (bank_found == case.expect_bank) if case.expect_bank is not None else None,
            "statements_raw": raw,
            "statements_kept_with_refs": qa.statements_with_refs,
            "ref_coverage": round(qa.statements_with_refs / raw, 3) if raw else (1.0 if draft.decision == "exclude" else 0.0),
            "unknown_refs": issues.count("unknown_ref"),
            "fabrication_issues": fabrication,
            "qa_status": qa.status,
            "qa_issue_codes": sorted(set(issues)),
            "cyrillic_ratio": round(_cyrillic_ratio(texts), 3),
            "headline": qa.headline[:160],
            "usage": usage.as_dict(),
            "wall_s": round(time.perf_counter() - started, 2),
        }
    )
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    ok = [r for r in rows if r.get("json_valid")]

    def avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 4) if values else None

    return {
        "cases": n,
        "json_valid_pct": round(100 * len(ok) / n, 1) if n else None,
        "json_valid_first_try_pct": round(100 * sum(1 for r in rows if r.get("json_valid_first_try")) / n, 1) if n else None,
        "decision_ok": f"{sum(1 for r in ok if r.get('decision_ok'))}/{n}",
        "event_status_ok": f"{sum(1 for r in ok if r.get('event_status_ok'))}/{sum(1 for r in ok if r.get('event_status_ok') is not None)}",
        "bank_ok": f"{sum(1 for r in ok if r.get('bank_ok'))}/{sum(1 for r in ok if r.get('bank_ok') is not None)}",
        "ref_coverage_avg": avg([r["ref_coverage"] for r in ok]),
        "fabrication_issues_total": sum(len(r.get("fabrication_issues", [])) for r in ok),
        "unknown_refs_total": sum(r.get("unknown_refs", 0) for r in ok),
        "russian_min_ratio": min((r["cyrillic_ratio"] for r in ok), default=None),
        "avg_latency_s": avg([r["usage"].get("latency_ms", 0) / 1000 for r in rows if r.get("usage")]),
        "avg_cost_rub_per_candidate": avg([r["usage"].get("cost_rub", 0.0) for r in rows if r.get("usage")]),
        "total_cost_rub": round(sum(r["usage"].get("cost_rub", 0.0) for r in rows if r.get("usage")), 4),
        "avg_prompt_tokens": avg([r["usage"].get("prompt_tokens", 0) for r in rows if r.get("usage")]),
        "avg_completion_tokens": avg([r["usage"].get("completion_tokens", 0) for r in rows if r.get("usage")]),
        "reasoning_tokens_total": sum(r["usage"].get("reasoning_tokens", 0) for r in rows if r.get("usage")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", default="z-ai/glm-5.3-flash,deepseek/deepseek-v4.1-flash,google/gemma-4-26b-a4b-it")
    parser.add_argument("--cases", default="")
    parser.add_argument("--max-rub", type=float, default=10.0, help="личный лимит процесса, ₽")
    parser.add_argument("--stop-above", type=float, default=250.0, help="не запускать, если расход команды выше, ₽")
    parser.add_argument("--out", default="")
    parser.add_argument("--dump-dir", default="", help="куда сохранить ответы моделей для ручного просмотра (не в репозиторий)")
    args = parser.parse_args(argv)

    settings = AnalystSettings.from_env()
    if not settings.has_key:
        print("LLM_API_KEY не задан — живое сравнение невозможно", file=sys.stderr)
        return 2
    ledger = BudgetLedger(settings.daily_budget_rub, session_cap_rub=args.max_rub)
    llm = LLMGateway(api_key=settings.api_key, base_url=settings.base_url, fallback_base_url=settings.fallback_base_url,
                     ledger=ledger, timeout_s=settings.timeout_s, max_retries=settings.max_retries)
    info = llm.refresh_remote_spend(force=True)
    spend_before = info["spend"] if info else None
    print(f"Расход команды до запуска: {spend_before} ₽")
    if spend_before is not None and spend_before > args.stop_above:
        print("Расход команды выше порога — живые запросы не выполняются", file=sys.stderr)
        return 3

    cases = [c for c in eval_cases() if not args.cases or c.case_id in args.cases.split(",")]
    rows: list[dict[str, Any]] = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        for case in cases:
            row = run_case(llm, model, case, dump_dir=Path(args.dump_dir) if args.dump_dir else None)
            rows.append(row)
            print(json.dumps({k: row.get(k) for k in ("model", "case_id", "json_valid", "decision_final", "ref_coverage", "fabrication_issues", "qa_issue_codes", "wall_s")}, ensure_ascii=False))
            if "usage" in row:
                print("   ", row.get("headline"), "|", row["usage"].get("cost_rub"), "₽")
    info = llm.refresh_remote_spend(force=True)
    spend_after = info["spend"] if info else None
    models = sorted({r["model"] for r in rows})
    result = {
        "generated_at": datetime.now(MSK).isoformat(timespec="seconds"),
        "prompt_version": prompts.PROMPT_VERSION,
        "as_of": AS_OF,
        "data": "синтетические профили и события (seed_data, seed_bundles, docs/b6-examples/conflict.json)",
        "team_spend_before_rub": spend_before,
        "team_spend_after_rub": spend_after,
        "session_cost_rub": round(ledger.session_spent_rub, 4),
        "summary": {m: summarize([r for r in rows if r["model"] == m]) for m in models},
        "rows": rows,
    }
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=1))
    print(f"Потрачено процессом: {result['session_cost_rub']} ₽; расход команды {spend_before} → {spend_after} ₽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
