"""Portable offline provenance explorer from an explicit pipeline export."""

import json
from pathlib import Path


def build_report(export, output):
    export, output = Path(export), Path(output)
    manifest = json.loads((export / "manifest.json").read_text())
    import hashlib

    for name, expected in manifest["outputs"].items():
        if hashlib.sha256((export / name).read_bytes()).hexdigest() != expected:
            raise ValueError("export_hash_mismatch:" + name)

    def rows(name):
        return [
            json.loads(line)
            for line in (export / name).read_text().splitlines()
            if line.strip()
        ]

    data = {
        "documents": rows("documents.jsonl"),
        "mentions": rows("mentions.jsonl"),
        "clusters": rows("clusters.jsonl"),
        "decisions": rows("decisions.jsonl"),
        "audit": rows("audit.jsonl"),
        "report": json.loads((export / "report.json").read_text()),
    }
    template = Path(__file__).with_name("report.html").read_text()
    payload = (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace("&", "\\u0026")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.replace("/*PIPELINE_DATA*/", payload))
    return str(output)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True)
    parser.add_argument("--output", required=True)
    a = parser.parse_args()
    print(build_report(a.export, a.output))
