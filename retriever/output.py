"""Publish a complete batch/trace handoff directory or leave no ready output."""

import os
import shutil
import tempfile
from pathlib import Path

from .evaluation import atomic_json
from .models import canonical, text_hash


def write_run(run, output):
    output = Path(output)
    if output.exists():
        raise ValueError("output_already_exists_use_new_directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix="." + output.name + "-", dir=output.parent)
    )
    try:
        payloads = {"batch.json": run.batch, "trace.json": run.trace}
        hashes = {}
        for name, value in payloads.items():
            content = canonical(value) + "\n"
            with (temporary / name).open("w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            hashes[name] = text_hash(content)
        atomic_json(
            temporary / "manifest.json",
            {
                "schema_version": "retriever-output.v1",
                "request_id": run.batch.request_id,
                "index_generation": run.batch.index_generation,
                "status": run.batch.status,
                "files": hashes,
            },
        )
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
