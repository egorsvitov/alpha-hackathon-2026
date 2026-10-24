"""Метрики качества и стоимости аналитика по сохранённому кэшу (без сети, без расхода бюджета).

Запуск из ``backend``::

    python -m app.analyst.eval.metrics                       # кэш репозитория app/analyst/cache
    python -m app.analyst.eval.metrics --cache-dir <папка>   # другой каталог (например, рабочий кэш)
    python -m app.analyst.eval.metrics --no-write --json     # только JSON в stdout, файл не пишется

Считаются: доля валидного JSON (и с первой попытки), покрытие утверждений ссылками на
данные, распределение решений include / watch / exclude, токены, время ответа,
стоимость в ₽ на кандидата и срабатывания QA. Срабатывания QA по умолчанию
пересчитываются текущим валидатором по сохранённым ответам модели (так видно, как
правки валидатора влияют на старые ответы); если кандидат устарел (изменились данные
или профиль компании), берётся сохранённый результат QA и кандидат считается в ``stale``.

Результат печатается таблицей и сохраняется в ``eval/metrics_latest.json``. В файле нет
ключей, абсолютных путей и времени запуска, поэтому он воспроизводим.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from app.analyst.llm_gateway import mask_secrets
from app.analyst.settings import PACKAGE_DIR

DEFAULT_CACHE_DIR = PACKAGE_DIR / "cache"
DEFAULT_OUT = Path(__file__).resolve().parent / "metrics_latest.json"
DEFAULT_AS_OF = "2026-10-22T12:00:00+03:00"
DECISIONS = ("include", "watch", "exclude")
LEVELS = ("high", "medium", "low")


# --------------------------------------------------------------------------- чтение кэша


def load_envelopes(cache_dir: Path) -> list[dict[str, Any]]:
    """Все записи кэша (конверты ``{key, model, created_at, record}``), повреждённые файлы пропускаются."""
    out: list[dict[str, Any]] = []
    if not cache_dir.is_dir():
        return out
    for path in sorted(cache_dir.rglob("*.json")):
        try:
            env = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(env, dict) and isinstance(env.get("record"), dict) and "key" in env:
            out.append(env)
    return out


def _statements(insight: dict[str, Any]) -> list[dict[str, Any]]:
    """Утверждения карточки, которые должны опираться на данные."""
    out = list(insight.get("facts") or [])
    for name in ("company_relevance", "impact_mechanism", "ai_conclusion"):
        if insight.get(name):
            out.append(insight[name])
    for name in ("risks", "opportunities", "next_steps"):
        out += list(insight.get(name) or [])
    return out


def _has_refs(statement: dict[str, Any]) -> bool:
    return any(statement.get(k) for k in ("claim_ids", "feature_ids", "evidence_ids", "bank_chunk_ids"))


def _draft_statements(draft: dict[str, Any]) -> list[dict[str, Any]]:
    out = list(draft.get("what_happened") or [])
    for name in ("company_relevance", "impact_mechanism", "ai_conclusion"):
        if isinstance(draft.get(name), dict):
            out.append(draft[name])
    for name in ("risks", "opportunities", "next_steps"):
        out += [s for s in (draft.get(name) or []) if isinstance(s, dict)]
    return [s for s in out if isinstance(s, dict)]


def _draft_has_refs(statement: dict[str, Any]) -> bool:
    return bool(statement.get("claim_ids") or statement.get("feature_ids"))


# --------------------------------------------------------------------------- пересчёт QA


class QaRecomputer:
    """Пересчёт QA текущим валидатором по сохранённому ответу модели (офлайн)."""

    def __init__(self, as_of: str) -> None:
        from app.analyst.input_adapter import bank_fragments_from_evidence, build_analyst_input, parse_dt
        from app.services.seed_data import COMPANIES_DATA, EVENTS_DATA, SHARED_EVIDENCE

        self.as_of = as_of
        self.as_of_dt = parse_dt(as_of)
        self.companies = {c["company_id"]: c for c in COMPANIES_DATA}
        self.events = {e["event_id"]: e for e in EVENTS_DATA}
        self.bank = bank_fragments_from_evidence(SHARED_EVIDENCE)
        self._build = build_analyst_input

    def issues_for(self, env: dict[str, Any]) -> tuple[str, list[dict[str, Any]]] | None:
        """``(qa_status, issues)`` или ``None``, если пересчитать нельзя (кандидат устарел)."""
        from app.analyst.analyst import cache_key
        from app.analyst.qa import validate_draft
        from app.analyst.schemas import LLMDraft
        from app.analyst.view import build_prompt_view

        parts = str(env.get("key", "")).split("|")
        if len(parts) != 6 or self.as_of_dt is None:
            return None
        company_id, ref, role, profile_version, prompt_version, model = parts
        company, event = self.companies.get(company_id), self.events.get(ref.split("@")[0])
        draft = (env["record"].get("run") or {}).get("draft")
        if company is None or event is None or not isinstance(draft, dict):
            return None
        try:
            inp = self._build(company, role, [event], as_of=self.as_of_dt, bank_corpus=self.bank)
            cand = inp.candidates[0]
            if cache_key(company_id, cand, role, profile_version, prompt_version, model) != env["key"]:
                return None  # данные события или профиль изменились после расчёта
            qa = validate_draft(LLMDraft.model_validate(draft), build_prompt_view(inp, cand), role, as_of=self.as_of)
        except Exception:  # noqa: BLE001 - метрики не должны падать из-за одной записи
            return None
        return qa.status, [i.model_dump() for i in qa.issues]


# --------------------------------------------------------------------------- расчёт


def _pct(part: float, total: float) -> float | None:
    return round(100.0 * part / total, 1) if total else None


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def _round(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def compute_metrics(envelopes: Iterable[dict[str, Any]], *, recompute_qa: bool = True, as_of: str = DEFAULT_AS_OF) -> dict[str, Any]:
    from app.analyst.schemas import LLMDraft

    envs = list(envelopes)
    recomputer = QaRecomputer(as_of) if (recompute_qa and envs) else None

    models: Counter[str] = Counter()
    prompts_seen: Counter[str] = Counter()
    valid = first_try = repairs = invalid = 0
    stmt_total = stmt_refs = model_stmt_total = model_stmt_refs = 0
    model_decisions: Counter[str] = Counter()
    final_decisions: Counter[str] = Counter()
    significance: Counter[str] = Counter()
    prompt_tokens = completion_tokens = reasoning_tokens = 0
    latencies: list[float] = []
    cost = tariff_cost = 0.0
    with_usage = 0
    qa_status: Counter[str] = Counter()
    qa_codes: Counter[str] = Counter()
    qa_severity: Counter[str] = Counter()
    with_issues = stale = 0
    bank_found = 0
    per_model: dict[str, dict[str, Any]] = {}

    for env in envs:
        record = env["record"]
        run = record.get("run") or {}
        model = env.get("model") or run.get("model") or "unknown"
        models[model] += 1
        prompts_seen[str(run.get("prompt_version") or "unknown")] += 1
        pm = per_model.setdefault(model, {"candidates": 0, "valid": 0, "first_try": 0, "stmts": 0, "refs": 0, "cost": 0.0,
                                          "tokens": 0, "latency": [], "usage_n": 0})
        pm["candidates"] += 1

        usage = run.get("usage") or {}
        draft = run.get("draft")
        draft_ok = False
        if isinstance(draft, dict):
            try:
                LLMDraft.model_validate(draft)
                draft_ok = True
            except Exception:  # noqa: BLE001
                draft_ok = False
        if draft_ok:
            valid += 1
            pm["valid"] += 1
            if not int(usage.get("json_repairs") or 0):
                first_try += 1
                pm["first_try"] += 1
        else:
            invalid += 1
        repairs += int(usage.get("json_repairs") or 0)

        if draft_ok:
            model_decisions[str(draft.get("decision"))] += 1
            for s in _draft_statements(draft):
                model_stmt_total += 1
                model_stmt_refs += 1 if _draft_has_refs(s) else 0

        insight, excluded = record.get("insight"), record.get("excluded")
        if insight:
            final_decisions[str(insight.get("decision"))] += 1
            significance[str(insight.get("significance"))] += 1
            for s in _statements(insight):
                stmt_total += 1
                stmt_refs += 1 if _has_refs(s) else 0
                pm["stmts"] += 1
                pm["refs"] += 1 if _has_refs(s) else 0
            if (insight.get("bank_assessment") or {}).get("status") in ("found", "outdated"):
                bank_found += 1
        elif excluded:
            final_decisions["exclude" if excluded.get("reason_code") == "not_relevant" else "excluded_by_rules"] += 1
            significance["excluded"] += 1

        if usage.get("calls"):
            with_usage += 1
            pm["usage_n"] += 1
            prompt_tokens += int(usage.get("prompt_tokens") or 0)
            completion_tokens += int(usage.get("completion_tokens") or 0)
            reasoning_tokens += int(usage.get("reasoning_tokens") or 0)
            cost += float(usage.get("cost_rub") or 0.0)
            tariff_cost += float(usage.get("cost_rub_tariff") or 0.0)
            latencies.append(float(usage.get("latency_ms") or 0))
            pm["cost"] += float(usage.get("cost_rub") or 0.0)
            pm["tokens"] += int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
            pm["latency"].append(float(usage.get("latency_ms") or 0))

        status, issues = str(run.get("qa_status") or "skipped"), list(run.get("qa_issues") or [])
        if recomputer is not None:
            recomputed = recomputer.issues_for(env)
            if recomputed is None:
                stale += 1
            else:
                status, issues = recomputed
        qa_status[status] += 1
        real = [i for i in issues if i.get("severity") != "info"]
        if real:
            with_issues += 1
        for issue in real:
            qa_codes[str(issue.get("code"))] += 1
            qa_severity[str(issue.get("severity"))] += 1

    n = len(envs)
    total_tokens = prompt_tokens + completion_tokens
    return {
        "candidates": n,
        "models": dict(sorted(models.items())),
        "prompt_versions": dict(sorted(prompts_seen.items())),
        "json": {
            "valid_pct": _pct(valid, n),
            "first_try_pct": _pct(first_try, n),
            "invalid": invalid,
            "repairs_total": repairs,
        },
        "reference_coverage": {
            "statements": stmt_total,
            "with_refs": stmt_refs,
            "pct": _pct(stmt_refs, stmt_total),
            "model_statements": model_stmt_total,
            "model_with_refs": model_stmt_refs,
            "model_pct": _pct(model_stmt_refs, model_stmt_total),
            "unknown_refs_removed": qa_codes.get("unknown_ref", 0),
        },
        "decisions": {
            "model": {d: model_decisions.get(d, 0) for d in DECISIONS},
            "final": {**{d: final_decisions.get(d, 0) for d in DECISIONS},
                      **({"excluded_by_rules": final_decisions["excluded_by_rules"]} if final_decisions.get("excluded_by_rules") else {})},
            "model_share_pct": {d: _pct(model_decisions.get(d, 0), sum(model_decisions.values())) for d in DECISIONS},
        },
        "significance": {k: significance.get(k, 0) for k in (*LEVELS, "excluded")},
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "reasoning": reasoning_tokens,
            "total": total_tokens,
            "per_candidate": round(total_tokens / with_usage) if with_usage else None,
        },
        "latency_ms": {
            "avg": round(statistics.fmean(latencies)) if latencies else None,
            "median": round(statistics.median(latencies)) if latencies else None,
            "p95": round(_quantile(latencies, 0.95)) if latencies else None,
            "max": round(max(latencies)) if latencies else None,
        },
        "cost_rub": {
            "total": _round(cost),
            "per_candidate": _round(cost / with_usage) if with_usage else None,
            "tariff_total": _round(tariff_cost),
            "tariff_per_candidate": _round(tariff_cost / with_usage) if with_usage else None,
        },
        "qa": {
            "source": "recomputed" if recomputer is not None else "stored",
            "stale": stale,
            "passed": qa_status.get("passed", 0),
            "passed_with_fixes": qa_status.get("passed_with_fixes", 0),
            "failed": qa_status.get("failed", 0),
            "candidates_with_issues": with_issues,
            "issues_total": sum(qa_codes.values()),
            "triggered_pct": _pct(with_issues, n),
            "by_severity": dict(sorted(qa_severity.items())),
            "by_code": dict(qa_codes.most_common()),
        },
        "bank_position_found": bank_found,
        "per_model": {
            m: {
                "candidates": v["candidates"],
                "json_valid_pct": _pct(v["valid"], v["candidates"]),
                "json_first_try_pct": _pct(v["first_try"], v["candidates"]),
                "reference_coverage_pct": _pct(v["refs"], v["stmts"]),
                "cost_rub_per_candidate": _round(v["cost"] / v["usage_n"]) if v["usage_n"] else None,
                "tokens_per_candidate": round(v["tokens"] / v["usage_n"]) if v["usage_n"] else None,
                "latency_ms_avg": round(statistics.fmean(v["latency"])) if v["latency"] else None,
            }
            for m, v in sorted(per_model.items())
        },
    }


# --------------------------------------------------------------------------- вывод


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def format_table(m: dict[str, Any], source: str) -> str:
    rows: list[tuple[str, str]] = [
        ("Источник", source),
        ("Кандидатов в кэше", _fmt(m["candidates"])),
        ("Модели", ", ".join(f"{k} x{v}" for k, v in m["models"].items()) or "—"),
        ("Версии промпта", ", ".join(f"{k} x{v}" for k, v in m["prompt_versions"].items()) or "—"),
        ("Валидный JSON", _fmt(m["json"]["valid_pct"], "%")),
        ("Валидный JSON с 1-й попытки", _fmt(m["json"]["first_try_pct"], "%")),
        ("Ремонтов JSON (всего)", _fmt(m["json"]["repairs_total"])),
        ("Покрытие ссылками (после QA)", _fmt(m["reference_coverage"]["pct"], "%")
         + f" ({m['reference_coverage']['with_refs']}/{m['reference_coverage']['statements']})"),
        ("Покрытие ссылками (ответ модели)", _fmt(m["reference_coverage"]["model_pct"], "%")),
        ("Решения модели include/watch/exclude", "/".join(str(m["decisions"]["model"][d]) for d in DECISIONS)),
        ("Решения после QA include/watch/exclude", "/".join(str(m["decisions"]["final"][d]) for d in DECISIONS)),
        ("Значимость high/medium/low/исключено", "/".join(str(m["significance"][k]) for k in (*LEVELS, "excluded"))),
        ("Токены всего (вход/выход)", f"{m['tokens']['total']} ({m['tokens']['prompt']}/{m['tokens']['completion']})"),
        ("Токенов на кандидата", _fmt(m["tokens"]["per_candidate"])),
        ("Время ответа, мс (сред./медиана/p95)", " / ".join(_fmt(m["latency_ms"][k]) for k in ("avg", "median", "p95"))),
        ("Стоимость всего, ₽ (факт шлюза)", _fmt(m["cost_rub"]["total"])),
        ("Стоимость на кандидата, ₽", _fmt(m["cost_rub"]["per_candidate"])),
        ("Стоимость по тарифу на кандидата, ₽", _fmt(m["cost_rub"]["tariff_per_candidate"])),
        ("QA: passed/с исправлениями/failed", f"{m['qa']['passed']}/{m['qa']['passed_with_fixes']}/{m['qa']['failed']}"
         + f" ({m['qa']['source']}, устарело {m['qa']['stale']})"),
        ("QA: кандидатов с замечаниями", _fmt(m["qa"]["triggered_pct"], "%") + f" ({m['qa']['candidates_with_issues']})"),
        ("QA: срабатываний (ошибки/предупреждения)",
         f"{m['qa']['by_severity'].get('error', 0)}/{m['qa']['by_severity'].get('warn', 0)}"),
        ("Позиция банка найдена", _fmt(m["bank_position_found"])),
    ]
    width = max(len(k) for k, _ in rows)
    lines = [f"{k.ljust(width)} | {v}" for k, v in rows]
    if m["qa"]["by_code"]:
        lines.append("")
        lines.append("Срабатывания QA по кодам:")
        code_w = max(len(c) for c in m["qa"]["by_code"])
        lines += [f"  {c.ljust(code_w)} | {n}" for c, n in m["qa"]["by_code"].items()]
    if m["per_model"]:
        lines.append("")
        lines.append("По моделям: модель | кандидатов | JSON % | с 1-й попытки % | ссылки % | ₽/канд. | токенов/канд. | мс")
        for name, v in m["per_model"].items():
            lines.append(
                f"  {name} | {v['candidates']} | {_fmt(v['json_valid_pct'])} | {_fmt(v['json_first_try_pct'])} | "
                f"{_fmt(v['reference_coverage_pct'])} | {_fmt(v['cost_rub_per_candidate'])} | {_fmt(v['tokens_per_candidate'])} | "
                f"{_fmt(v['latency_ms_avg'])}"
            )
    return "\n".join(lines)


def display_path(path: Path) -> str:
    """Путь без домашнего каталога пользователя (в отчёт попадает только относительный вид)."""
    try:
        return path.resolve().relative_to(PACKAGE_DIR.parents[1]).as_posix()
    except ValueError:
        return path.name


def build_report(cache_dir: Path, *, recompute_qa: bool = True, as_of: str = DEFAULT_AS_OF) -> dict[str, Any]:
    metrics = compute_metrics(load_envelopes(cache_dir), recompute_qa=recompute_qa, as_of=as_of)
    return {"source": display_path(cache_dir), "as_of": as_of, **metrics}


def write_report(report: dict[str, Any], out: Path) -> None:
    text = mask_secrets(json.dumps(report, ensure_ascii=False, indent=1)) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="каталог с сохранёнными ответами")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="куда сохранить JSON")
    parser.add_argument("--as-of", default=os.environ.get("ANALYST_AS_OF") or DEFAULT_AS_OF, help="момент анализа, с которым считался кэш")
    parser.add_argument("--no-recompute", action="store_true", help="не пересчитывать QA, взять сохранённые результаты")
    parser.add_argument("--no-write", action="store_true", help="не записывать JSON-файл")
    parser.add_argument("--json", action="store_true", help="печатать JSON вместо таблицы")
    args = parser.parse_args(argv)

    if not args.cache_dir.is_dir():
        print(f"Каталог кэша не найден: {display_path(args.cache_dir)}", file=sys.stderr)
        return 2
    report = build_report(args.cache_dir, recompute_qa=not args.no_recompute, as_of=args.as_of)
    if args.json:
        print(mask_secrets(json.dumps(report, ensure_ascii=False, indent=1)))
    else:
        if report["candidates"] == 0:
            print(f"Кэш пуст: {report['source']}")
        else:
            print(format_table(report, report["source"]))
    if not args.no_write:
        write_report(report, args.out)
        if not args.json:
            print(f"\nJSON сохранён: {display_path(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
