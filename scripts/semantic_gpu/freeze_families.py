"""Freeze conservative named story families BEFORE viewing threshold outputs.
Unassigned material is exploratory, never an independent test. The resulting
assignments must be source-reviewed; keyword rules cannot guarantee no leakage.
"""

import json
import re
from collections import Counter
from experiment import ROOT, save

RULES = [
    ("train", "market_manipulation", r"манипулирования рынком"),
    ("dev", "witkoff_talks", r"уиткофф"),
    ("dev", "turkey_rate", r"(?:ЦБ|банк).{0,8}Турци"),
    ("dev", "mars_expedition", r"на Марс\b"),
    ("test", "starshield", r"starshield"),
    ("test", "valieva", r"валиев"),
    ("test", "rail_compensation", r"бряндин"),
    ("test", "sparrow_song", r"рыжеворотничкового воробья"),
]


def freeze():
    output = ROOT / "story-splits.json"
    if output.exists():
        raise ValueError("frozen split already exists; preserve it")
    docs = json.loads((ROOT / "run/plan.json").read_text())["documents"]
    assignment = {}
    for d in docs:
        hits = [
            (s, f)
            for s, f, pattern in RULES
            if re.search(
                pattern, d["article"]["title"] + "\n" + d["article"]["text"], re.I
            )
        ]
        if d["article"]["source"].startswith("moex_"):
            hits.append(("train", "exchange_notices"))
        splits = {s for s, f in hits}
        assignment[d["document_id"]] = {
            "split": next(iter(splits))
            if len(splits) == 1
            else "excluded_bridge"
            if splits
            else "exploratory",
            "families": [f for s, f in hits],
        }
    hashes = {}
    for d in docs:
        hashes.setdefault(d["text_sha256"], []).append(d["document_id"])
    for ids in hashes.values():
        explicit = [
            assignment[i] for i in ids if assignment[i]["split"] != "exploratory"
        ]
        if explicit:
            value = (
                explicit[0]
                if len({a["split"] for a in explicit}) == 1
                else {
                    "split": "excluded_bridge",
                    "families": sorted({f for a in explicit for f in a["families"]}),
                }
            )
            for i in ids:
                assignment[i] = value
    save(
        output,
        {
            "status": "single_annotator_draft",
            "annotator": "Codex source-reading draft; no independent human adjudication",
            "frozen_before_threshold_comparison": True,
            "rules": RULES,
            "assignment": assignment,
            "limitations": [
                "Only named families are held out; keyword rules may miss paraphrases.",
                "Family membership is not a positive pair label.",
                "No claim of complete leakage elimination or independent test for the full corpus.",
            ],
        },
    )
    print(Counter(a["split"] for a in assignment.values()))


if __name__ == "__main__":
    freeze()
