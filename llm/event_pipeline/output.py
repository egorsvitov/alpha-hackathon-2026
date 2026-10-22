"""Atomic snapshot export and explicit-label evaluation."""

import json
import hashlib
import tempfile
import os
import shutil
from contextlib import closing
import sqlite3
from pathlib import Path
from .pipeline import read, connect, load_mentions, status, locked, canonical_json


def export_run(root, output):
    root, output = Path(root), Path(output)
    with locked(root):
        if output.exists():
            raise ValueError("export_exists")
        plan = read(root / "plan.json")
        mentions = load_mentions(root)
        with connect(root) as db:
            steps = [
                json.loads(r[0])
                for r in db.execute("SELECT payload FROM steps ORDER BY position")
            ]
            audits = [
                {"document_id": d, **json.loads(p)}
                for d, p in db.execute("SELECT * FROM extraction")
            ]
        clusters = {s["cluster"]["cluster_id"]: s["cluster"] for s in steps}
        processed = {s["mention_id"] for s in steps}
        for m in mentions:
            if m.mention_id not in processed:
                cid = "unassessed:" + m.mention_id
                clusters[cid] = {
                    "cluster_id": cid,
                    "version": 1,
                    "mention_ids": [m.mention_id],
                    "decisions": [],
                    "label": m.gloss or cid,
                    "label_kind": "navigation",
                    "verification_status": "unassessed",
                }
        report = status(root)
        report["complete"] = (
            report["documents"] == report["extracted_documents"]
            and len(processed) == len(mentions)
            and all(s["reason"] in ("assessed", "no_candidates") for s in steps)
            and all(
                p.get("audit") is not None and not p["audit"]["rejected"]
                for a in audits
                for p in a["packets"]
            )
        )
        report["semantic_support"] = "not_independently_assessed"
        with closing(sqlite3.connect(root / "jobs.sqlite3")) as jobdb:
            calls = [
                dict(
                    zip(
                        (
                            "call_id",
                            "started_at",
                            "finished_at",
                            "state",
                            "response_json",
                            "error_json",
                        ),
                        r,
                    )
                )
                for r in jobdb.execute(
                    "SELECT call_id,created_at,finished_at,state,response_json,error_json FROM llm_calls ORDER BY created_at,call_id"
                )
            ]
            artifacts = [
                {"artifact_id": aid, "kind": kind, "payload": json.loads(payload)}
                for aid, kind, payload in jobdb.execute(
                    "SELECT artifact_id,kind,payload_json FROM artifacts ORDER BY created_at,artifact_id"
                )
            ]
        for c in calls:
            for key in ("response_json", "error_json"):
                c[key.removesuffix("_json")] = (
                    json.loads(c.pop(key)) if c[key] else None
                )
        reservations = [a for a in artifacts if a["kind"] == "shared_quota_reservation"]
        if reservations:
            by_journal = {}
            for a in reservations:
                by_journal.setdefault(a["payload"]["journal"], set()).add(
                    a["payload"]["call_id"]
                )
            for journal, ids in by_journal.items():
                with closing(
                    sqlite3.connect(
                        Path(journal).resolve().as_uri() + "?mode=ro", uri=True
                    )
                ) as q:
                    for cid, created, response, error, elapsed in q.execute(
                        "SELECT call_id,created_at,response_json,error_json,elapsed_seconds FROM calls"
                    ):
                        if cid in ids:
                            calls.append(
                                {
                                    "call_id": cid,
                                    "started_at": created,
                                    "finished_at": created + elapsed
                                    if elapsed is not None
                                    else None,
                                    "state": "finished"
                                    if elapsed is not None
                                    else "reserved",
                                    "response": json.loads(response)
                                    if response
                                    else None,
                                    "error": json.loads(error) if error else None,
                                }
                            )
        report["requests"] = len(calls)
        report["tokens"] = {
            "input": 0,
            "output": 0,
            "reasoning_reported_separately": 0,
            "unknown_usage_calls": 0,
        }
        for c in calls:
            usage = (c.get("response") or {}).get("body", {}).get("usage")
            if not isinstance(usage, dict):
                report["tokens"]["unknown_usage_calls"] += 1
                continue
            for token_kind, key in [
                ("input", "prompt_tokens"),
                ("output", "completion_tokens"),
            ]:
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    report["tokens"][token_kind] += value
            reasoning = (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens", 0
            )
            if type(reasoning) is int:
                report["tokens"]["reasoning_reported_separately"] += reasoning
        report["elapsed_provider_seconds"] = sum(
            c["finished_at"] - c["started_at"]
            for c in calls
            if c.get("finished_at") is not None
        )
        report["unresolved_mentions"] = (
            sum(
                s["reason"] != "no_candidates"
                and (
                    s["reason"] != "assessed"
                    or any(
                        d["identity"] == "uncertain" for d in s["cluster"]["decisions"]
                    )
                )
                for s in steps
            )
            + len(mentions)
            - len(processed)
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".export-", dir=output.parent))
        try:
            payloads = {
                "mentions.jsonl": [m.model_dump() for m in mentions],
                "clusters.jsonl": list(clusters.values()),
                "decisions.jsonl": steps,
                "audit.jsonl": audits,
                "documents.jsonl": plan["documents"],
                "calls.jsonl": calls,
                "artifacts.jsonl": artifacts,
            }
            hashes = {}
            for name, rows in payloads.items():
                raw = "".join(canonical_json(r) + "\n" for r in rows).encode()
                (tmp / name).write_bytes(raw)
                hashes[name] = hashlib.sha256(raw).hexdigest()
            (tmp / "report.json").write_text(canonical_json(report))
            hashes["report.json"] = hashlib.sha256(
                (tmp / "report.json").read_bytes()
            ).hexdigest()
            (tmp / "manifest.json").write_text(
                canonical_json(
                    {
                        "schema_version": "event-pipeline.v1",
                        "run_id": plan["run_id"],
                        "configuration": plan["configuration"],
                        "outputs": hashes,
                        "complete": report["complete"],
                    }
                )
            )
            os.rename(tmp, output)
        except BaseException:
            shutil.rmtree(tmp)
            raise
        return report


def evaluate_run(root, gold):
    """Only explicitly labeled mentions/pairs count. No closed-world negatives."""
    gold = read(gold) if isinstance(gold, (str, Path)) else gold
    mentions = load_mentions(root)
    with connect(root) as db:
        steps = [
            json.loads(r[0])
            for r in db.execute("SELECT payload FROM steps ORDER BY position")
        ]
    groups = {s["mention_id"]: s["cluster"]["cluster_id"] for s in steps}
    matches = {}
    for g in gold["mentions"]:
        found = [
            m.mention_id
            for m in mentions
            if m.document_id == g["document_id"]
            and set(g["block_ids"]) <= set(m.block_ids)
        ]
        matches[g["id"]] = found
    labelled = [g for g in gold["mentions"] if g.get("event_id") is not None]
    events = {g["event_id"] for g in labelled}
    survived = {g["event_id"] for g in labelled if matches[g["id"]]}
    pair_results = []
    for p in gold.get("pairs", []):
        if p["identity"] == "uncertain":
            continue
        a, b = matches[p["left"]], matches[p["right"]]
        predicted = any(groups.get(x, x) == groups.get(y, y) for x in a for y in b)
        pair_results.append(
            {
                "left": p["left"],
                "right": p["right"],
                "expected": p["identity"],
                "same": predicted,
                "detected": bool(a and b),
            }
        )
    detected = sum(bool(v) for v in matches.values())
    positives = [p for p in pair_results if p["expected"] == "same"]
    predictions = [p for p in pair_results if p["same"]]
    true_positive = sum(p["expected"] == "same" for p in predictions)
    matching_predicted = {mid for ids in matches.values() for mid in ids}
    exhaustive = set(gold.get("exhaustive_document_ids", []))
    exhaustive_predictions = [m for m in mentions if m.document_id in exhaustive]
    precision = (
        sum(m.mention_id in matching_predicted for m in exhaustive_predictions)
        / len(exhaustive_predictions)
        if exhaustive_predictions
        else None
    )
    retrieval = []
    by_mid = {mid: g for g in gold["mentions"] for mid in matches[g["id"]]}
    earlier = set()
    for step in steps:
        current = by_mid.get(step["mention_id"])
        if current and current.get("event_id"):
            expected = {
                g["id"]
                for g in gold["mentions"]
                if g.get("event_id") == current["event_id"] and g["id"] in earlier
            }
            if expected:
                retrieved = {r["mention_id"] for r in step["retrieval"]["mentions"]}
                retrieval.append(
                    any(retrieved.intersection(matches[g]) for g in expected)
                )
            earlier.add(current["id"])
    return {
        "mention_precision_localization": precision,
        "same_precision_on_labeled_pairs": true_positive / len(predictions)
        if predictions
        else None,
        "same_recall_on_labeled_pairs": true_positive / len(positives)
        if positives
        else None,
        "candidate_recall_eligible_mentions": len(retrieval),
        "candidate_recall_on_detected_mentions": sum(retrieval) / len(retrieval)
        if retrieval
        else None,
        "gold_mentions": len(matches),
        "detected_mentions": detected,
        "mention_recall": detected / len(matches) if matches else None,
        "event_loss_rate": 1 - len(survived) / len(events) if events else None,
        "pairs": pair_results,
        "unresolved_rate": sum(
            s["cluster"].get("verification_status") in ("uncertain", "unassessed")
            for s in steps
        )
        / len(steps)
        if steps
        else None,
        "false_merges": sum(
            p["same"] and p["expected"] == "different" for p in pair_results
        ),
        "false_splits": sum(
            not p["same"] and p["expected"] == "same" and p["detected"]
            for p in pair_results
        ),
        "limitations": [
            "Block overlap measures localization, not semantic identity. Unlabelled pairs are not negatives."
        ],
    }
