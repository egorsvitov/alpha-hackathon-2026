"""Freeze a diverse group audit sample. Annotation is a separate source-reading step."""

import argparse
from collections import Counter
import json
from pathlib import Path
import random
from experiment import ROOT, SEED, rows, save


def sample(comparison, count=24):
    base = Path(comparison)
    units = {u["unit_id"]: u for u in rows(base / "units.jsonl")}
    docs = {d["document_id"]: d for d in rows(base / "documents.jsonl")}
    gs = []
    time_signatures = {
        tuple(sorted(g["unit_ids"])) for g in rows(base / "semantic_time.groups.jsonl")
    }
    for variant in ("semantic", "semantic_time"):
        for g in rows(base / (variant + ".groups.jsonl")):
            ds = {units[u]["document_id"] for u in g["unit_ids"]}
            sources = {docs[d]["article"]["source"] for d in ds}
            gs.append(
                {
                    **g,
                    "variant": variant,
                    "documents": len(ds),
                    "sources": sorted(sources),
                    "differs_with_time": tuple(sorted(g["unit_ids"]))
                    not in time_signatures
                    if variant == "semantic"
                    else False,
                }
            )
    rng = random.Random(SEED)
    for_shuffle = list(gs)
    rng.shuffle(for_shuffle)
    selected = []
    seen = set()

    def take(category, candidates, n):
        for g in candidates:
            signature = tuple(sorted(g["unit_ids"]))
            if signature in seen:
                continue
            selected.append(
                {"case_id": f"group-{len(selected) + 1:02d}", "category": category, **g}
            )
            seen.add(signature)
            n -= 1
            if n == 0:
                break

    take(
        "largest_partial_if_needed",
        sorted(gs, key=lambda g: (-len(g["unit_ids"]), g["group_id"])),
        1,
    )
    take("large", [g for g in for_shuffle if 5 <= len(g["unit_ids"]) <= 10], 3)
    take("singletons", [g for g in for_shuffle if len(g["unit_ids"]) == 1], 4)
    take(
        "cross_source",
        [
            g
            for g in for_shuffle
            if len(g["sources"]) > 1 and 2 <= len(g["unit_ids"]) <= 4
        ],
        6,
    )
    take(
        "changed_by_time",
        [
            g
            for g in for_shuffle
            if g["differs_with_time"] and 2 <= len(g["unit_ids"]) <= 5
        ],
        4,
    )
    take(
        "same_document_fragments",
        [
            g
            for g in for_shuffle
            if g["documents"] == 1 and 2 <= len(g["unit_ids"]) <= 4
        ],
        3,
    )
    take(
        "random_background",
        [g for g in for_shuffle if 2 <= len(g["unit_ids"]) <= 4],
        count - len(selected),
    )
    save(
        ROOT / "review/selection.json",
        {
            "seed": SEED,
            "status": "single_annotator_draft",
            "comparison": str(base),
            "algorithm": "Seeded category sampling before labelling; duplicate memberships removed; largest group included even if review is partial.",
            "cases": selected,
            "categories": dict(Counter(g["category"] for g in selected)),
        },
    )
    packets = []
    edges = {
        variant: rows(base / (variant + ".neighbors.jsonl"))
        for variant in ("semantic", "semantic_time")
    }
    for g in selected:
        neighbor_context = []
        if len(g["unit_ids"]) == 1:
            uid = g["unit_ids"][0]
            candidate = next(
                (
                    e
                    for e in edges[g["variant"]]
                    if e["left"] == uid
                    and units[e["right"]]["document_id"] != units[uid]["document_id"]
                ),
                None,
            )
            if candidate:
                neighbor_context.append(
                    {"edge": candidate, "unit": units[candidate["right"]]}
                )
        packets.append(
            {
                **g,
                "units": [units[u] for u in g["unit_ids"]],
                "nearest_other_document": neighbor_context,
                "documents": [
                    docs[d]
                    for d in sorted({units[u]["document_id"] for u in g["unit_ids"]})
                ],
            }
        )
    save(ROOT / "review/packets.json", packets)
    return {
        "cases": len(selected),
        "categories": dict(Counter(g["category"] for g in selected)),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--comparison", required=True)
    print(json.dumps(sample(**vars(p.parse_args()))))
