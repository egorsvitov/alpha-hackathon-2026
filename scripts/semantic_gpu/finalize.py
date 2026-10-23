"""Validate exports, aggregate measurements and package LOCAL source-bearing artifacts."""

import argparse
from collections import Counter
import hashlib
import json
from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
from experiment import ROOT, rows, save, quantiles


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while block := f.read(1024**2):
            h.update(block)
    return h.hexdigest()


def collect():
    data = {"comparisons": {}, "cache": {}, "metrics": {}}
    for base in sorted((ROOT / "comparisons").iterdir()):
        if not (base / "manifest.json").exists():
            continue
        manifest = json.loads((base / "manifest.json").read_text())
        for name, expected in manifest["outputs"].items():
            if sha(base / name) != expected:
                raise ValueError(f"export hash mismatch: {base / name}")
        unit_ids = {u["unit_id"] for u in rows(base / "units.jsonl")}
        if {v["unit_id"] for v in rows(base / "vectors.jsonl")} != unit_ids:
            raise ValueError("vector coverage mismatch")
        for variant in ("semantic", "semantic_time"):
            assigned = [
                u
                for g in rows(base / (variant + ".groups.jsonl"))
                for u in g["unit_ids"]
            ]
            if len(assigned) != len(unit_ids) or set(assigned) != unit_ids:
                raise ValueError("membership coverage mismatch")
        data["comparisons"][base.name] = {
            "report": json.loads((base / "report.json").read_text()),
            "configuration": manifest["configuration"],
            "export_bytes": sum(
                p.stat().st_size for p in base.iterdir() if p.is_file()
            ),
        }
    for name in ("run", "full-run"):
        with sqlite3.connect(ROOT / name / "vectors.sqlite3") as db:
            counts = [r[0] for r in db.execute("SELECT tokens FROM vectors")]
            batch = [
                dict(zip(("texts", "tokens", "seconds"), r))
                for r in db.execute("SELECT texts,tokens,seconds FROM batches")
            ]
        seconds = sum(b["seconds"] for b in batch)
        data["cache"][name] = {
            "bytes": (ROOT / name / "vectors.sqlite3").stat().st_size,
            "vectors": len(counts),
            "tokens": sum(counts),
            "tokens_per_text": quantiles(counts),
            "batch_seconds": quantiles([b["seconds"] for b in batch]),
            "total_batch_seconds": seconds,
            "texts_per_second": len(counts) / seconds if seconds else None,
            "tokens_per_second": sum(counts) / seconds if seconds else None,
            "batches": len(batch),
        }
    for p in sorted((ROOT / "metrics").glob("*.json")):
        m = json.loads(p.read_text())
        m.pop("samples", None)
        data["metrics"][p.stem] = m
    journal = ROOT / "summaries/quota.sqlite3"
    if journal.exists():
        with sqlite3.connect(journal.resolve().as_uri() + "?mode=ro", uri=True) as db:
            calls = []
            for request, response, error, elapsed in db.execute(
                "SELECT request_json,response_json,error_json,elapsed_seconds FROM calls ORDER BY created_at"
            ):
                calls.append(
                    {
                        "request": json.loads(request),
                        "response": json.loads(response) if response else None,
                        "error": json.loads(error) if error else None,
                        "elapsed_seconds": elapsed,
                    }
                )
            cap = db.execute("SELECT max_requests FROM settings WHERE id=1").fetchone()[
                0
            ]
        unique = {}
        runs = {}
        for directory in sorted(ROOT.glob("summaries*")):
            if not (directory / "results.json").is_file():
                continue
            entries = json.loads((directory / "results.json").read_text())
            runs[directory.name] = {
                "statuses": dict(Counter(r["status"] for r in entries)),
                "reasons": dict(
                    Counter(r.get("reason") for r in entries if r.get("reason"))
                ),
            }
            for result in entries:
                unique[result["group_id"]] = result
        results = list(unique.values())
        totals = Counter()
        provider_cost = Decimal("0")
        cost_entries = 0
        for c in calls:
            usage = (c.get("response") or {}).get("body", {}).get("usage", {}) or {}
            for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(usage.get(field), int):
                    totals[field] += usage[field]
            totals["reasoning_tokens"] += (
                usage.get("completion_tokens_details") or {}
            ).get("reasoning_tokens", 0)
            totals["cached_prompt_tokens"] += (
                usage.get("prompt_tokens_details") or {}
            ).get("cached_tokens", 0)
            if isinstance(usage.get("cost"), (int, float, str)):
                provider_cost += Decimal(str(usage["cost"]))
                cost_entries += 1
        data["api"] = {
            "calls": len(calls),
            "shared_cap": cap,
            "unsettled_calls": sum(c["elapsed_seconds"] is None for c in calls),
            "http_responses": dict(
                Counter(
                    str(c["response"]["status_code"]) for c in calls if c["response"]
                )
            ),
            "transport_errors": dict(
                Counter(c["error"]["code"] for c in calls if c["error"])
            ),
            "transport_seconds": sum(c["elapsed_seconds"] or 0 for c in calls),
            "provider_cost_sum": str(provider_cost),
            "provider_cost_entries": cost_entries,
            "provider_cost_currency": None,
            "runs": runs,
            "group_status": dict(Counter(r["status"] for r in results)),
            "reasons": dict(
                Counter(r.get("reason") for r in results if r.get("reason"))
            ),
            "usage": dict(totals),
            "cost_rub": None,
            "cost_status": "No verified RUB tariffs; inspect raw provider usage/cost fields.",
            "format_reference_checks": sum(
                bool(r.get("output", {}).get("references_exist")) for r in results
            ),
            "semantic_support": "Separately source-reviewed examples only; not implied by valid references.",
        }
    if (ROOT / "review/annotations.json").exists():
        a = json.loads((ROOT / "review/annotations.json").read_text())
        data["manual_review"] = {
            "groups": len(a["cases"]),
            "group_labels": dict(Counter(c["label"] for c in a["cases"])),
            "prelabelled_dev_pairs": len(
                json.loads((ROOT / "dev-pairs.json").read_text())["pairs"]
            ),
            "prelabelled_test_pairs": len(
                json.loads((ROOT / "test-pairs.json").read_text())["pairs"]
            ),
            "status": "single_annotator_draft; Codex source reading; no independent human adjudication",
        }
    data["git_commit"] = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True
    ).strip()
    save(ROOT / "aggregate.json", data)
    return data


def package():
    from llm.events.extraction.admin_cli import load_cli_env
    import os

    load_cli_env(Path("llm/events/.env"))
    key = os.environ.get("LLM_API_KEY", "").encode()
    source = ROOT / "code"
    source.mkdir(exist_ok=True)
    for base in (Path("scripts/semantic_gpu"), Path("llm/event_pipeline/semantic")):
        for p in base.iterdir():
            if not p.is_file() or p.suffix not in (".py", ".md", ".txt"):
                continue
            target = source / base / p.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
    included = []
    for p in sorted(ROOT.rglob("*")):
        if (
            not p.is_file()
            or p.name == "ARTIFACT_MANIFEST.json"
            or p.name.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".lock"))
        ):
            continue
        if p.name == ".env":
            raise ValueError("Refusing to archive .env")
        if key and key in p.read_bytes():
            raise ValueError("Secret detected in artifact; refusing archive")
        included.append(p)
    manifest = {
        "schema_version": "semantic-gpu-artifacts.v1",
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "privacy": "LOCAL archive contains original news and API requests. Do not commit or publish.",
        "excluded": [
            "working SQLite databases",
            "locks",
            "model weights and temporary download chunks outside experiment root",
            "secrets",
        ],
        "projection_caution": "Future 2D projection distances are not original cosine distances.",
        "files": {
            str(p.relative_to(ROOT)): {"bytes": p.stat().st_size, "sha256": sha(p)}
            for p in included
        },
    }
    save(ROOT / "ARTIFACT_MANIFEST.json", manifest)
    archive = ROOT.parent / "handoff/gpu-experiment-20261023.tar.gz"
    with tarfile.open(archive, "w:gz", compresslevel=5) as tar:
        for p in included + [ROOT / "ARTIFACT_MANIFEST.json"]:
            tar.add(
                p, arcname="gpu-experiment/" + str(p.relative_to(ROOT)), recursive=False
            )
    Path(str(archive) + ".sha256").write_text(sha(archive) + "  " + archive.name + "\n")
    print(
        json.dumps(
            {
                "archive": str(archive),
                "bytes": archive.stat().st_size,
                "sha256": sha(archive),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--package", action="store_true")
    args = p.parse_args()
    collect()
    if args.package:
        package()
