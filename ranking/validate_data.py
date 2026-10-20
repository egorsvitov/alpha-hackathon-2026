"""Normalize the whole supplied table and check ranking invariants on a sample.

This is a data/integration smoke check, NOT a relevance-quality benchmark.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import time
from .engine import rank_events
from .events import load_events, timestamp
from .profiles import CompanyRepository, DEFAULT_COMPANIES, ROOT


def validate(companies_csv=DEFAULT_COMPANIES, max_rank_companies=100):
    if max_rank_companies < 1:
        raise ValueError("Размер выборки должен быть положительным")
    started = time.perf_counter()
    repository = CompanyRepository(companies_csv)
    load_seconds = time.perf_counter()-started
    ids = sorted(repository.company_ids-repository.conflicts)
    step = max(1, len(ids)//max_rank_companies)
    chosen = set(ids[::step][:max_rank_companies])
    examples = ROOT/"data/examples/ranking"
    events = load_events(examples/"events.json")
    as_of = "2026-10-20T18:00:00+03:00"
    industries, missing, samples = Counter(), Counter(), []
    normalized = 0
    for uid in ids:
        profile = repository.get(uid)
        normalized += 1
        industries[profile["industry"] or "unknown"] += 1
        missing.update(profile["missing_fields"])
        for key, feature in profile["features"].items():
            if key != feature["feature_id"] or feature["source"]["file_sha256"] != repository.sha256:
                raise AssertionError(f"Нарушен provenance: {uid}")
        if uid in chosen:
            samples.append(profile)
    normalized_seconds = time.perf_counter()-started-load_seconds
    durations, empty = [], 0
    for profile in samples:
        tick = time.perf_counter()
        result = rank_events(profile, events, as_of)
        durations.append((time.perf_counter()-tick)*1000)
        empty += not bool(result["feed"])
        if len({r["cluster_id"] for r in result["feed"]}) != len(result["feed"]):
            raise AssertionError("Дубли кластера в ленте")
        if len(result["feed"]) > 5:
            raise AssertionError("Нарушен top-k")
        for row in result["ranked"]:
            if not math.isfinite(row["score"]) or not 0 <= row["score"] <= 100:
                raise AssertionError("Некорректный score")
            if timestamp(row["event"]["published_at"]) > timestamp(as_of):
                raise AssertionError("Утечка будущей публикации")
            expected = round(min(100, sum(r["contribution"] for r in row["reasons"])), 4)
            if row["score"] != expected:
                raise AssertionError("Score не восстанавливается по объяснению")
            for ref in row["features_used"]:
                if row["features_used"][ref] != profile["features"][ref]:
                    raise AssertionError("Признак не соответствует профилю")
    durations.sort()
    def percentile(p):
        return round(durations[round((len(durations)-1)*p)], 3) if durations else None
    return {"kind": "full_table_normalization_and_sample_ranking_invariants",
            "relevance_quality_measured": False, "source_sha256": repository.sha256,
            "unique_company_ids": len(repository.company_ids), "conflicting_ids_excluded": sorted(repository.conflicts),
            "normalized_profiles": normalized, "industry_counts": dict(industries), "missing_field_counts": dict(missing),
            "ranked_sample_companies": len(samples), "empty_sample_feeds": empty,
            "events_input": len(events), "as_of": as_of,
            "csv_load_seconds": round(load_seconds, 3), "all_profiles_normalization_seconds": round(normalized_seconds, 3),
            "rank_only_p50_ms": percentile(.5), "rank_only_p95_ms": percentile(.95),
            "latency_scope": "single local process, loaded profiles, synthetic 20-message pool; excludes ingestion, LLM and network",
            "status": "passed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companies-csv", default=str(DEFAULT_COMPANIES))
    parser.add_argument("--max-rank-companies", type=int, default=100)
    parser.add_argument("--output", default=str(ROOT/"data/processed/ranking/data_validation.json"))
    args = parser.parse_args()
    try:
        result = validate(args.companies_csv, args.max_rank_companies)
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, AssertionError) as error:
        parser.exit(2, f"Ошибка проверки: {error}\n")
