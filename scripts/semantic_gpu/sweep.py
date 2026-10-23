"""Run immutable comparisons; select parameters using ONLY pre-frozen dev pairs."""

import argparse
import hashlib
import json
import math
import subprocess
import sys
from experiment import ROOT, save
from evaluate import evaluate


def compare(label, threshold, alpha=0.4, half_life=7, *, full=False):
    output = ROOT / "comparisons" / label
    if output.exists():
        raise ValueError("comparison already exists; preserve outputs")
    extra = []
    if full:
        import psutil

        units = len(json.loads((ROOT / "full-run/plan.json").read_text())["units"])
        estimate = 40 * units**2
        available = psutil.virtual_memory().available
        save(
            ROOT / "full-grouping-resource-assessment.json",
            {
                "units": units,
                "estimated_matrix_bytes": estimate,
                "available_ram_bytes": available,
                "max_units": 6000,
                "max_matrix_mb": 1200,
                "decision": "proceed only with at least 3x estimated working matrices available",
            },
        )
        if available < 3 * estimate:
            raise RuntimeError("Insufficient RAM for full comparison")
        extra = ["--max-units", "6000", "--max-matrix-mb", "1200"]
    subprocess.run(
        [
            sys.executable,
            "scripts/semantic_gpu/experiment.py",
            "compare",
            "--label",
            label,
            "--run",
            str(ROOT / ("full-run" if full else "run")),
            "--threshold",
            str(threshold),
            "--alpha",
            str(alpha),
            "--half-life",
            str(half_life),
        ]
        + extra,
        check=True,
    )
    ev = evaluate(output, ROOT / "dev-pairs.json")
    save(ROOT / "evaluation" / (label + "-dev.json"), ev)
    return ev


def rank(ev):
    return (-ev["negative_pairs_joined"], ev["positive_pairs_joined"])


def main(stage):
    choices = []
    if stage == "thresholds":
        for t in (0.65, 0.75, 0.85):
            label = "t" + str(t).replace(".", "")
            ev = compare(label, t)
            choices.append({"threshold": t, "label": label, "dev": ev["semantic"]})
        chosen = max(choices, key=lambda c: (rank(c["dev"]), c["threshold"]))
        save(
            ROOT / "chosen-threshold.json",
            {
                "chosen": chosen,
                "grid": choices,
                "dev_labels_sha256": hashlib.sha256(
                    (ROOT / "dev-pairs.json").read_bytes()
                ).hexdigest(),
            },
        )
    elif stage == "temporal":
        t = json.loads((ROOT / "chosen-threshold.json").read_text())["chosen"][
            "threshold"
        ]
        for alpha in (0.2, 0.4, 0.6):
            for half in (1, 7, 30):
                label = "a" + str(alpha).replace(".", "") + "_h" + str(half)
                ev = compare(label, t, alpha, half)
                choices.append(
                    {
                        "threshold": t,
                        "alpha": alpha,
                        "half_life_days": half,
                        "label": label,
                        "dev": ev["semantic_time"],
                    }
                )
        chosen = max(
            choices,
            key=lambda c: (
                rank(c["dev"]),
                -(
                    abs(c["alpha"] - 0.4)
                    + 0.05 * abs(math.log2(c["half_life_days"] / 7))
                ),
            ),
        )
        save(
            ROOT / "chosen-temporal.json",
            {"chosen": chosen, "grid": choices, "test_unused": True},
        )
    elif stage == "full":
        chosen = json.loads((ROOT / "chosen-temporal.json").read_text())["chosen"]
        compare(
            "full-chosen",
            chosen["threshold"],
            chosen["alpha"],
            chosen["half_life_days"],
            full=True,
        )
    else:
        raise ValueError("unknown stage")
    print(json.dumps({k: v for k, v in chosen.items() if k != "dev"}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=["thresholds", "temporal", "full"])
    main(p.parse_args().stage)
