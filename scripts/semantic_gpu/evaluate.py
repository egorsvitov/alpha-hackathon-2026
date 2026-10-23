"""Evaluate only explicit labelled document pairs; export group diagnostics."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from experiment import ROOT, save, rows


def evaluate(comparison, labels):
    root = Path(comparison)
    units = {u["unit_id"]: u for u in rows(root / "units.jsonl")}
    labelled = json.loads(Path(labels).read_text())["pairs"]
    result = {}
    for variant in ("semantic", "semantic_time"):
        membership = defaultdict(set)
        for g in rows(root / (variant + ".groups.jsonl")):
            for u in g["unit_ids"]:
                membership[units[u]["document_id"]].add(g["group_id"])
        cases = []
        for a, b, label, reason in labelled:
            together = bool(membership[a] & membership[b])
            cases.append(
                {
                    "left": a,
                    "right": b,
                    "label": label,
                    "together": together,
                    "reason": reason,
                }
            )
        positives = [p for p in cases if p["label"] == "useful_together"]
        negatives = [p for p in cases if p["label"] == "unrelated_material"]
        result[variant] = {
            "explicit_positive_pairs": len(positives),
            "positive_pairs_joined": sum(p["together"] for p in positives),
            "explicit_negative_pairs": len(negatives),
            "negative_pairs_joined": sum(p["together"] for p in negatives),
            "positive_link_recall": sum(p["together"] for p in positives)
            / len(positives)
            if positives
            else None,
            "cases": cases,
            "unlabelled_pairs": "unknown, excluded from metrics",
            "criterion": "At least one co-grouped fragment from each document; not whole-document equivalence",
        }
    return result


def diagnostics(comparison):
    root = Path(comparison)
    units = {u["unit_id"]: u for u in rows(root / "units.jsonl")}
    docs = {d["document_id"]: d for d in rows(root / "documents.jsonl")}
    out = {}
    for variant in ("semantic", "semantic_time"):
        groups = rows(root / (variant + ".groups.jsonl"))
        details = []
        for g in groups:
            docids = {units[u]["document_id"] for u in g["unit_ids"]}
            dates = sorted(
                {
                    units[u]["publication"]["value"][:10]
                    for u in g["unit_ids"]
                    if units[u]["publication"]["value"]
                }
            )
            details.append(
                {
                    "group_id": g["group_id"],
                    "units": len(g["unit_ids"]),
                    "documents": len(docids),
                    "sources": sorted({docs[d]["article"]["source"] for d in docids}),
                    "dates": dates,
                    "unit_ids": g["unit_ids"],
                    "minimum_pair_score": g["minimum_pair_score"],
                }
            )
        out[variant] = {
            "size_histogram": dict(Counter(len(g["unit_ids"]) for g in groups)),
            "cross_document_groups": sum(g["documents"] > 1 for g in details),
            "cross_source_groups": sum(len(g["sources"]) > 1 for g in details),
            "multi_day_groups": sum(len(g["dates"]) > 1 for g in details),
            "largest_groups": sorted(
                details, key=lambda g: (-g["units"], g["group_id"])
            )[:20],
        }
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--comparison", required=True)
    p.add_argument("--labels", default=str(ROOT / "dev-pairs.json"))
    p.add_argument("--output", required=True)
    args = p.parse_args()
    save(
        args.output,
        {
            "pairs": evaluate(args.comparison, args.labels),
            "diagnostics": diagnostics(args.comparison),
        },
    )
