"""Check the real publication boundary and preserved geometry before Git push."""

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from publish_report import ASSETS, load_payload, public_payload


def verify(source, published):
    private = load_payload(source)
    public = load_payload(published)
    notes = json.loads((ASSETS / "public_notes.json").read_text())
    assert public["public"] is True
    for did, document in public["documents"].items():
        assert document["text"] == "" and document["title"] == "Документ #" + did[:8]
        assert "url" not in document
    for unit in public["units"]:
        assert unit["text"] == "" and "embedding_text" not in unit
    for call in public["calls"]:
        assert set(call["request"]) <= {"model", "thinking", "max_tokens"}
        assert call["content"] is None
    assert public["coordinates"] == private["coordinates"]
    assert public["groups"] == private["groups"]
    assert [u["unit_id"] for u in public["units"]] == [u["unit_id"] for u in private["units"]]
    edges_checked = 0
    for variant, neighbors in public["neighbors"].items():
        for i, edges in enumerate(neighbors):
            assert len(edges) == len(private["neighbors"][variant][i])
            for edge, original in zip(edges, private["neighbors"][variant][i]):
                assert edge[0] == original["i"]
                for actual, key in zip(edge[1:], ("cosine", "gap_days", "time_multiplier", "score")):
                    if original[key] is None:
                        assert actual is None
                    else:
                        assert abs(actual - original[key]) <= .00000050001
                edges_checked += 1
    # Regression probe: replace every source-bearing field with a unique poison
    # value. Neither original nor synthetic text may cross the export boundary.
    poisoned = deepcopy(private)
    marker = "PRIVATE_SOURCE_SENTINEL_7d4c3bd3_НЕ_ПУБЛИКОВАТЬ"
    poisoned["report_text"] = marker
    poisoned["artifacts"] = [{"name": marker, "href": marker}]
    for document in poisoned["documents"].values():
        for key in ("title", "text", "url"):
            document[key] = marker
    for unit in poisoned["units"]:
        unit["text"] = unit["embedding_text"] = marker
    for call in poisoned["calls"]:
        call["request"]["messages"] = [{"role": "user", "content": marker}]
        call["content"] = marker
        if call.get("error"):
            call["error"]["message"] = marker
    for summary in poisoned["summaries"].values():
        if summary.get("assessment"):
            summary["assessment"]["reason"] = marker
        if summary.get("output"):
            s = summary["output"]["summary"]
            s["theme"] = marker
            s["limitations"] = [marker]
            for key in ("items", "differences", "contradictions"):
                for claim in s[key]:
                    claim["text"] = marker
    for case in poisoned["reviews"]["cases"]:
        case["reason"] = marker
    for tuning in (poisoned["thresholds"], poisoned["temporal"]):
        for point in [tuning["chosen"], *tuning["grid"]]:
            for case in point.get("dev", {}).get("cases", []):
                case["reason"] = marker
    safe_probe = public_payload(poisoned, notes)
    assert marker not in json.dumps(safe_probe, ensure_ascii=False)
    assert safe_probe == public_payload(private, notes)
    manifest = json.loads(published.with_name("manifest.json").read_text())
    for name, sha in manifest["files"].items():
        assert hashlib.sha256(published.with_name(name).read_bytes()).hexdigest() == sha
    result = {"status": "passed", "units": len(public["units"]), "edges_checked": edges_checked,
              "coordinates_and_membership_unchanged": True, "scores_within_rounding_tolerance": True,
              "source_text_title_url_absent": True, "api_prompts_content_absent": True,
              "poisoned_source_regression": "passed", "manifest_hashes_verified": True,
              "tested_html_sha256": hashlib.sha256(published.read_bytes()).hexdigest()}
    published.with_name("privacy-check.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/processed/event_pipeline/interactive-report-20261023/index.html"))
    parser.add_argument("--report", type=Path, default=Path("docs/reports/semantic-gpu-20261023/index.html"))
    args = parser.parse_args()
    verify(args.source, args.report)
