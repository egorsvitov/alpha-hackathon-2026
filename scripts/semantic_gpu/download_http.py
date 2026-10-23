"""Resumable bounded HTTP range download from the pinned OFFICIAL HF revision.
Each invocation attempts a missing part once. Full size and LFS SHA are checked
before the verified blob is installed in the ordinary Hugging Face cache.
"""

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
from experiment import ROOT, REVISION, Measure, save

URL = "https://huggingface.co/BAAI/bge-m3/resolve/" + REVISION + "/pytorch_model.bin"
DOWNLOAD_URL = os.environ.get("MODEL_WEIGHTS_URL", URL)
SIZE = 2271145830
SHA = "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38"
CHUNK = 4 * 1024**2
PARTS = Path(".cache/semantic-http-parts")


def fetch(index):
    start = index * CHUNK
    end = min(SIZE, start + CHUNK) - 1
    path = PARTS / f"{index:04d}.part"
    if path.exists() and path.stat().st_size == end - start + 1:
        return {"index": index, "status": "cached", "bytes": end - start + 1}
    t = time.monotonic()
    try:
        req = urllib.request.Request(
            DOWNLOAD_URL + "?download=true&part=" + str(index),
            headers={"Range": f"bytes={start}-{end}"},
        )
        with urllib.request.urlopen(req, timeout=35) as response:
            if (
                response.status != 206
                or response.headers.get("Content-Range")
                != f"bytes {start}-{end}/{SIZE}"
            ):
                raise ValueError("range_not_honoured")
            with path.with_suffix(".tmp").open("wb") as f:
                while block := response.read(256 * 1024):
                    f.write(block)
        if path.with_suffix(".tmp").stat().st_size != end - start + 1:
            raise ValueError("part_length_mismatch")
        path.with_suffix(".tmp").rename(path)
        return {
            "index": index,
            "status": "downloaded",
            "bytes": end - start + 1,
            "seconds": time.monotonic() - t,
        }
    except Exception as exc:
        return {
            "index": index,
            "status": "failed",
            "error_type": type(exc).__name__,
            "seconds": time.monotonic() - t,
        }


def main():
    PARTS.mkdir(parents=True, exist_ok=True)
    stamp = str(int(time.time()))
    result = []
    with Measure(ROOT / f"metrics/download-http-{stamp}.json"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for item in pool.map(fetch, range((SIZE + CHUNK - 1) // CHUNK)):
                result.append(item)
                if len(result) % 10 == 0:
                    print(
                        json.dumps(
                            {
                                "parts": len(result),
                                "successful": sum(
                                    x["status"] != "failed" for x in result
                                ),
                                "last": item,
                            }
                        ),
                        flush=True,
                    )
                save(ROOT / f"download-http-{stamp}.json", result)
        if any(x["status"] == "failed" for x in result):
            raise RuntimeError(
                "Some parts failed; inspect audit before explicitly resuming"
            )
        blob = Path(os.environ["HF_HOME"]) / "hub/models--BAAI--bge-m3/blobs" / SHA
        temporary = blob.with_suffix(".http-verified-tmp")
        h = hashlib.sha256()
        with temporary.open("wb") as out:
            for i in range(len(result)):
                with (PARTS / f"{i:04d}.part").open("rb") as f:
                    while data := f.read(1024**2):
                        h.update(data)
                        out.write(data)
        if h.hexdigest() != SHA or temporary.stat().st_size != SIZE:
            raise ValueError("LFS hash mismatch")
        os.replace(temporary, blob)
        snapshot = blob.parent.parent / "snapshots" / REVISION
        link = snapshot / "pytorch_model.bin"
        if not link.exists():
            link.symlink_to("../../blobs/" + SHA)
        save(
            ROOT / "model-http-verification.json",
            {
                "pinned_source": URL,
                "transfer_source": DOWNLOAD_URL,
                "size": SIZE,
                "sha256": SHA,
                "verified": True,
                "chunk_bytes": CHUNK,
                "workers": 12,
                "metadata_source": "https://huggingface.co/api/models/BAAI/bge-m3/tree/"
                + REVISION,
            },
        )


if __name__ == "__main__":
    main()
