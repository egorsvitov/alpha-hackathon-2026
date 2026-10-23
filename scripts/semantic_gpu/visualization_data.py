"""Flatten immutable memberships and variant changes for a future UI; no 2D projection."""

import argparse
from collections import Counter
import itertools
import json
from pathlib import Path
from experiment import ROOT, rows, save


def export(comparison):
    base = Path(comparison)
    out = ROOT / "visualization"
    out.mkdir(exist_ok=True)
    units = {u["unit_id"]: u for u in rows(base / "units.jsonl")}
    docs = {d["document_id"]: d for d in rows(base / "documents.jsonl")}
    memberships = {}
    pairs = {}
    metrics = []
    for variant in ("semantic", "semantic_time"):
        groups = rows(base / (variant + ".groups.jsonl"))
        memberships[variant] = {u: g["group_id"] for g in groups for u in g["unit_ids"]}
        pairs[variant] = {
            pair
            for g in groups
            for pair in itertools.combinations(sorted(g["unit_ids"]), 2)
        }
        for g in groups:
            ds = {units[u]["document_id"] for u in g["unit_ids"]}
            dates = sorted(
                {
                    units[u]["publication"]["value"]
                    for u in g["unit_ids"]
                    if units[u]["publication"]["value"]
                }
            )
            metrics.append(
                {
                    "variant": variant,
                    "group_id": g["group_id"],
                    "unit_count": len(g["unit_ids"]),
                    "document_count": len(ds),
                    "source_counts": dict(
                        Counter(docs[d]["article"]["source"] for d in ds)
                    ),
                    "publication_min": dates[0] if dates else None,
                    "publication_max": dates[-1] if dates else None,
                    "minimum_pair_score": g["minimum_pair_score"],
                }
            )
    with (out / "membership.jsonl").open("w") as f:
        for uid, u in sorted(units.items()):
            f.write(
                json.dumps(
                    {
                        "unit_id": uid,
                        "document_id": u["document_id"],
                        **{v: m[uid] for v, m in memberships.items()},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    (out / "group-metrics.jsonl").write_text(
        "".join(json.dumps(g, ensure_ascii=False) + "\n" for g in metrics)
    )
    lost = sorted(pairs["semantic"] - pairs["semantic_time"])
    gained = sorted(pairs["semantic_time"] - pairs["semantic"])
    save(
        out / "pair-changes.json",
        {
            "semantic_pairs": len(pairs["semantic"]),
            "semantic_time_pairs": len(pairs["semantic_time"]),
            "lost_unit_pairs": lost,
            "gained_unit_pairs": gained,
            "warning": "Complete-link can reassign members; temporal groups need not refine semantic groups.",
        },
    )
    save(
        out / "index.json",
        {
            "schema_version": "semantic-visualization-input.v1",
            "comparison": str(base.relative_to(ROOT)),
            "files": {
                k: str((base / (k + ".jsonl")).relative_to(ROOT))
                for k in [
                    "vectors",
                    "units",
                    "documents",
                    "semantic.groups",
                    "semantic_time.groups",
                    "semantic.neighbors",
                    "semantic_time.neighbors",
                ]
            },
            "dimension": 1024,
            "normalized": True,
            "original_distance": "cosine; temporal scores additionally use publication gaps",
            "group_ids": "Scoped to the immutable run and configuration, not stable event IDs across snapshots",
            "projection": "Not calculated. Future 2D distances must not be treated as original distances.",
            "summary_runs": "Read summary-active.json and all preserved summary experiment configurations/audits; join on group_id.",
        },
    )
    print(
        json.dumps(
            {"units": len(units), "lost_pairs": len(lost), "gained_pairs": len(gained)}
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--comparison", required=True)
    export(p.parse_args().comparison)
