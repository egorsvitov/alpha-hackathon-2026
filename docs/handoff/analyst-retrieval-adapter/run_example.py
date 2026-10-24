"""Run the public retrieval adapter example locally, without calling an LLM."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from app.analyst.retrieval_adapter import adapt_retrieval_batch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Write AnalystInput and diagnostics to this directory")
    args = parser.parse_args()
    examples = ROOT / "docs/examples/retriever-analyst"

    def load(name: str):
        return json.loads((examples / (name + ".json")).read_text(encoding="utf-8"))

    result = adapt_retrieval_batch(load("batch"), load("company-profile"), load("active-interests"), "cfo")
    outputs = {"analyst-input": result.analyst_input.model_dump(mode="json"), "diagnostics": result.diagnostics()}
    for name, value in outputs.items():
        if value != load(name):
            raise SystemExit("Example differs from its reference: " + name)
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        for name, value in outputs.items():
            (args.output_dir / (name + ".json")).write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    print(json.dumps({"status": result.status, "candidates": len(result.analyst_input.candidates),
                      "states": [c["state"] for c in result.candidates],
                      "matches_reference": True, "llm_calls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
