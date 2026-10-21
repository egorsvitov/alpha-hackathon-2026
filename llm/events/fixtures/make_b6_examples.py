"""Author v0.2 handoff examples; legacy v0.1 files are never modified."""

from copy import deepcopy
import json
from pathlib import Path

from llm.events.compatibility import ROOT, upgrade_v01
from llm.events.fixtures.make_synthetic import build
from llm.events.validation import validate_grounding


def main():
    output = ROOT / "docs/b6-examples/v0.2"
    output.mkdir(parents=True, exist_ok=True)
    records = {}
    bundles = {}
    for path in sorted((ROOT / "docs/b6-examples").glob("*.json")):
        bundle = upgrade_v01(json.loads(path.read_text())).model_dump(mode="json")
        for claim in bundle["claims"]:
            for period in claim["time"].values():
                if period and period["text"] == "2026-10-20":
                    period["text"] = "20 октября 2026 года"
                if period and period["text"] == "2026-10-23":
                    period["text"] = "23 октября 2026 года"
        for source in bundle["sources"]:
            excerpt = next(e for e in bundle["evidence"] if e["source_id"] == source["source_id"])
            records[source["source_id"]] = {"source_id": source["source_id"], "article_version": source["article_version"],
                "article": {"article_id": source["article_id"], "source": source["publisher"], "url": source["url"],
                    "title": source["title"], "text": excerpt["quote"], "language": "ru",
                    "published_at": source["published_at"], "content_kind": source["content_kind"],
                    "is_full_original_text": source["is_full_original_text"], "is_synthetic": source["is_synthetic"]}}
        bundles[path.name] = bundle
    synthetic = build()
    for name, event_id in [("before-after.json", "synthetic:investment"), ("scenarios.json", "synthetic:scenarios"), ("cross-sentence.json", "synthetic:cross-sentence")]:
        bundle = deepcopy(next(b for b in synthetic["bundles"] if b["event"]["event_id"] == event_id))
        bundles[name] = bundle
        for record in synthetic["articles"]:
            if record["source_id"] in {s["source_id"] for s in bundle["sources"]}:
                records[record["source_id"]] = record
    raw_records = list(records.values())
    for name, bundle in bundles.items():
        validate_grounding(bundle, raw_records)
        (output / name).write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
    (Path(__file__).with_name("b6_articles.json")).write_text(json.dumps(raw_records, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {len(bundles)} v0.2 examples with versioned synthetic sources.")


if __name__ == "__main__":
    main()
