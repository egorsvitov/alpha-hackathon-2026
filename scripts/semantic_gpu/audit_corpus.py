"""Audit every exported embedding/assignment and prepare source-reading packets.

This checks mechanical correctness, not event identity or factual truth. All
source-bearing outputs must remain in ignored data/processed/event_pipeline/.
"""

import argparse
from collections import Counter, defaultdict
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
import time

import numpy as np
from threadpoolctl import threadpool_limits

from llm.event_pipeline.semantic.core import digest, interval, normalize, pair_score
from llm.event_pipeline.semantic.experiment import load_plan, make_units


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def read(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def audit(corpus, run, comparison, previous, output):
    started = time.perf_counter()
    if not output.resolve().is_relative_to((Path.cwd() / "data/processed/event_pipeline").resolve()):
        raise ValueError("Use the ignored event_pipeline output directory")
    output.mkdir(parents=True, exist_ok=True)
    manifest = read(corpus / "manifest.json")
    assert sha(corpus / "documents.jsonl") == manifest["outputs"]["documents.jsonl"]
    source = rows(corpus / "documents.jsonl")
    source_by_id = {d["document_id"]: d for d in source}
    assert len(source) == len(source_by_id) == manifest["documents"]
    plan = load_plan(run)
    assert {d["document_id"] for d in plan["documents"]} == set(source_by_id)
    exported = rows(comparison / "documents.jsonl")
    assert {d["document_id"]: d for d in exported} == source_by_id
    exported_manifest = read(comparison / "manifest.json")
    for name, expected in exported_manifest["outputs"].items():
        assert sha(comparison / name) == expected, name
    units_list = rows(comparison / "units.jsonl")
    units = {u["unit_id"]: u for u in units_list}
    assert len(units) == len(units_list)
    assert units == {u["unit_id"]: u for d in source for u in make_units(d, plan["max_chars"])}
    spans = defaultdict(list)
    for u in units_list:
        document = source_by_id[u["document_id"]]["article"]
        assert u["text"] == document[u["field"]][u["start"]:u["end"]]
        spans[u["document_id"], u["field"]].append((u["start"], u["end"]))
    uncovered = []
    for did, document in source_by_id.items():
        body = document["article"]["text"]
        field = "text" if body.strip() else "title"
        text = document["article"][field]
        cursor = 0
        for start, end in sorted(spans[did, field]):
            if text[cursor:start].strip():
                uncovered.append({"document_id": did, "start": cursor, "end": start})
            cursor = max(cursor, end)
        if text[cursor:].strip():
            uncovered.append({"document_id": did, "start": cursor, "end": len(text)})
    vector_rows = rows(comparison / "vectors.jsonl")
    vectors = {v["unit_id"]: v["vector"] for v in vector_rows}
    assert len(vectors) == len(vector_rows) and set(vectors) == set(units)
    ids = sorted(units)
    positions = {uid: i for i, uid in enumerate(ids)}
    matrix = np.asarray([vectors[uid] for uid in ids], dtype=np.float64)
    assert matrix.shape == (len(units), 1024) and np.isfinite(matrix).all()
    norm_error = float(np.max(np.abs(np.linalg.norm(matrix, axis=1) - 1)))
    assert norm_error < 1e-10
    profile = read(run / "encoder.json")
    text_keys = {u["text_hash"] for u in units_list}
    with sqlite3.connect((run / "vectors.sqlite3").resolve().as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        min_tokens, max_tokens, total_tokens = db.execute("SELECT MIN(tokens),MAX(tokens),SUM(tokens) FROM vectors").fetchone()
        assert max_tokens <= profile["max_tokens"] and min_tokens >= 0
        cache_data = {key: json.loads(vector) for key, vector in db.execute("SELECT key,vector FROM vectors")}
        expected_keys = {digest([profile, key]) for key in text_keys}
        assert set(cache_data) == expected_keys
        for uid in ids:
            # compare() normalizes a cached vector again before exporting it.
            assert vectors[uid] == normalize(cache_data[digest([profile, units[uid]["text_hash"]])])
    dates = [units[uid]["publication"] for uid in ids]
    intervals = [interval(d) for d in dates]
    starts = np.asarray([x[0] if x else np.nan for x in intervals])
    ends = np.asarray([x[1] if x else np.nan for x in intervals])
    configuration = exported_manifest["configuration"]
    report, groups_by_variant, edges_by_variant = {}, {}, {}
    with threadpool_limits(limits=4):
        for variant, alpha in (("semantic", 0), ("semantic_time", configuration["alpha"])):
            groups = rows(comparison / f"{variant}.groups.jsonl")
            groups_by_variant[variant] = groups
            members = [uid for g in groups for uid in g["unit_ids"]]
            assert len(members) == len(set(members)) == len(units) and set(members) == set(units)
            threshold_failures, minimum_mismatches, pair_count = [], [], 0
            for group in groups:
                indices = [positions[uid] for uid in group["unit_ids"]]
                n = len(indices)
                if n < 2:
                    assert group["minimum_pair_score"] is None
                    continue
                v = matrix[indices]
                scores = np.clip(v @ v.T, 0, 1)
                if alpha:
                    a, b = starts[indices], ends[indices]
                    gaps = np.maximum(0, np.maximum(a[:, None] - b, a - b[:, None])) / 86400
                    scores *= np.where(np.isnan(gaps), 1, 1-alpha+alpha*np.exp2(-gaps/configuration["half_life_days"]))
                minimum = float(np.min(scores[np.triu_indices(n, 1)]))
                pair_count += n*(n-1)//2
                if minimum < configuration["threshold"] - 1e-10:
                    threshold_failures.append(group["group_id"])
                if abs(minimum - group["minimum_pair_score"]) > 1e-10:
                    minimum_mismatches.append(group["group_id"])
            edges = rows(comparison / f"{variant}.neighbors.jsonl")
            edges_by_variant[variant] = edges
            by_left = Counter(e["left"] for e in edges)
            assert set(by_left) == set(units) and set(by_left.values()) == {configuration["neighbors"]}
            maximum_error = 0.0
            for e in edges:
                left, right = positions[e["left"]], positions[e["right"]]
                cosine = float(np.clip(matrix[left] @ matrix[right], -1, 1))
                a, b = intervals[left], intervals[right]
                gap = max(0, a[0]-b[1], b[0]-a[1])/86400 if a and b else None
                factor = 1 if gap is None else 1-alpha+alpha*2**(-gap/configuration["half_life_days"])
                for key, expected in (("cosine", cosine), ("gap_days", gap), ("time_multiplier", factor), ("score", max(0,cosine)*factor)):
                    if expected is None:
                        assert e[key] is None
                    else:
                        maximum_error = max(maximum_error, abs(expected-e[key]))
            assert maximum_error < 1e-10
            report[variant] = {"groups": len(groups), "singletons": sum(len(g["unit_ids"]) == 1 for g in groups),
                "largest_group": max(len(g["unit_ids"]) for g in groups), "within_group_pairs_checked": pair_count,
                "threshold_violations": threshold_failures, "minimum_score_mismatches": minimum_mismatches,
                "neighbors_checked": len(edges), "max_neighbor_error": maximum_error,
                "cross_source_groups": sum(len({source_by_id[units[uid]["document_id"]]["article"]["source"] for uid in g["unit_ids"]}) > 1 for g in groups)}
    stable = {name: sha(comparison / name) == sha(previous / name) for name in exported_manifest["outputs"] if name != "report.json"}
    result = {"status": "passed" if not uncovered and all(not v["threshold_violations"] and not v["minimum_score_mismatches"] for v in report.values()) else "failed",
        "corpus_documents": len(source), "units": len(units), "unique_embedding_texts": len(text_keys),
        "sources": dict(Counter(d["article"]["source"] for d in source)), "uncovered_nonwhitespace_spans": uncovered,
        "max_vector_norm_error": norm_error, "cache_integrity": "ok", "cache_keys_complete": True,
        "token_counts": {"min": min_tokens, "max": max_tokens, "total": total_tokens},
        "cache_bytes": (run / "vectors.sqlite3").stat().st_size,
        "max_model_tokens": profile["max_tokens"], "profile": profile, "variants": report,
        "matches_previous_export": stable, "corpus_sha256": sha(corpus / "documents.jsonl"),
        "comparison_manifest_sha256": sha(comparison / "manifest.json"), "seconds": time.perf_counter()-started,
        "scope": "Mechanical correctness over every unit/pair/neighbor; no semantic quality or factual-truth claim."}
    save(output / "coverage-audit.json", result)
    return units, source_by_id, vectors, groups_by_variant, edges_by_variant, result


def packets(selection, units, documents, vectors, groups, edges, output):
    memberships = {v: {uid: g for g in gs for uid in g["unit_ids"]} for v, gs in groups.items()}
    frozen, packets = [], []
    for spec in selection["cases"]:
        group = next(g for g in groups["semantic"] if g["group_id"].startswith(spec["group_prefix"]))
        chosen = list(group["unit_ids"])
        context = {}
        if spec.get("inspect_neighbor"):
            nearest = next(e for e in edges["semantic"] if e["left"] == chosen[0] and units[e["right"]]["document_id"] != units[chosen[0]]["document_id"])
            chosen.append(nearest["right"])
            other_group = memberships["semantic"][nearest["right"]]
            blocker = min(other_group["unit_ids"], key=lambda uid: sum(a*b for a,b in zip(vectors[chosen[0]],vectors[uid])))
            if blocker not in chosen:
                chosen.append(blocker)
            context = {"nearest_other_document": nearest, "neighbor_group_id": other_group["group_id"],
                       "neighbor_group_size": len(other_group["unit_ids"]), "blocker_unit_id": blocker,
                       "full_neighbor_group_read": False}
        frozen.append(dict(spec, group_id=group["group_id"], selected_unit_ids=chosen))
        pair_scores = []
        for a, b in itertools.combinations(chosen, 2):
            s = pair_score(vectors[a], vectors[b], units[a]["publication"], units[b]["publication"], .4, 7)
            pair_scores.append({"left": a, "right": b, **s,
                "same_semantic_group": memberships["semantic"][a]["group_id"] == memberships["semantic"][b]["group_id"],
                "same_time_group": memberships["semantic_time"][a]["group_id"] == memberships["semantic_time"][b]["group_id"]})
        packets.append({"case_id": spec["case_id"], "group": group, "context": context,
            "units": [dict(units[uid], memberships={v: memberships[v][uid]["group_id"] for v in memberships}) for uid in chosen],
            "documents": {did: documents[did] for did in {units[uid]["document_id"] for uid in chosen}},
            "pair_scores": pair_scores})
    save(output / "review/selection.json", dict(selection, cases=frozen))
    save(output / "review/packets.json", packets)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("data/processed/events/news-corpus-v1"))
    parser.add_argument("--run", type=Path, default=Path("data/processed/event_pipeline/gpu-experiment/full-run"))
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--previous", type=Path, default=Path("data/processed/event_pipeline/gpu-experiment/comparisons/full-chosen"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selection", type=Path)
    args = parser.parse_args()
    values = audit(args.corpus, args.run, args.comparison, args.previous, args.output)
    if args.selection:
        packets(read(args.selection), *values[:5], args.output)
    print(json.dumps(values[-1], ensure_ascii=False))
