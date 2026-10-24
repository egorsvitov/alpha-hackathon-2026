"""Build a deterministic public handoff ZIP from committed, allowlisted files."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import subprocess
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[3]
BASE = "ef84f539604f4d023b000e03389972eb07f432eb"


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    revision = git("rev-parse", args.revision).decode().strip()
    tracked = git("ls-tree", "-r", "--name-only", revision).decode().splitlines()
    required = {
        "backend/requirements.txt", "backend/requirements.lock",
        "backend/app/analyst/models.json", "backend/app/analyst/data/seed_bundles.json",
        "backend/app/schemas/base.py", "backend/app/services/seed_data.py",
        "backend/tests/__init__.py", "backend/tests/analyst/__init__.py",
        "backend/tests/analyst/helpers.py", "backend/tests/analyst/test_handoff8.py",
        "backend/tests/analyst/retrieval_fixtures.py", "backend/tests/analyst/test_retrieval_adapter.py",
        "docs/reviews/retriever-analyst-integration-20261024.md",
    }
    for path in tracked:
        p = PurePosixPath(path)
        if p.parent == PurePosixPath("backend/app/analyst") and p.suffix == ".py" and p.name != "precompute.py":
            required.add(path)
        if path.startswith(("backend/app/analyst/contracts/", "docs/examples/retriever-analyst/",
                            "docs/handoff/analyst-retrieval-adapter/")) and p.suffix in (".json", ".md", ".py"):
            required.add(path)
    missing = required - set(tracked)
    if missing:
        raise SystemExit("Required tracked files missing: " + ", ".join(sorted(missing)))
    files = {path: git("show", revision + ":" + path) for path in sorted(required)}
    files["README.md"] = files["docs/handoff/analyst-retrieval-adapter/README.md"]
    # Narrow pathspec ensures a rebuild never packs runtime artifacts or the ZIP.
    files["changes.patch"] = git("diff", "--binary", BASE, revision, "--", *sorted(required))
    manifest = {
        "format": "analyst-retrieval-adapter-handoff.v1", "repository": "egorsvitov/alpha-hackathon-2026",
        "branch": "feature/analyst-retrieval-adapter", "source_revision": revision, "base_revision": BASE,
        "implementation_revision": "661c0c4", "content_policy": "committed_allowlist_public_synthetic_only",
        "files": {path: {"sha256": sha256(data).hexdigest(), "bytes": len(data)} for path, data in sorted(files.items())},
    }
    files["MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path, data in sorted(files.items()):
            entry = ZipInfo(path, date_time=(2026, 10, 24, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    print(json.dumps({"archive": str(args.output), "source_revision": revision,
                      "files": len(files), "sha256": sha256(args.output.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
