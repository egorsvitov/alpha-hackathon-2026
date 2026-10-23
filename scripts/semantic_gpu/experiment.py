"""Reproducible local GPU experiment. Run from repository root with PYTHONPATH=."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import resource
import subprocess
import threading
import time

ROOT = Path(
    os.environ.get(
        "SEMANTIC_EXPERIMENT_ROOT", "data/processed/event_pipeline/gpu-experiment"
    )
)
REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
SEED = 20261023


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def rows(path):
    return [json.loads(x) for x in Path(path).read_text().splitlines() if x.strip()]


def quantiles(values):
    values = sorted(values)
    return (
        {
            str(p): values[round((len(values) - 1) * p / 100)]
            for p in (0, 25, 50, 75, 95, 99, 100)
        }
        if values
        else {}
    )


class Measure:
    """Sample total process RSS and device memory; CUDA allocator peaks are separate."""

    def __init__(self, path):
        self.path = path
        self.stop = threading.Event()
        self.samples = []

    def sample(self):
        import psutil

        proc = psutil.Process()
        while not self.stop.is_set():
            sample = {
                "elapsed": time.monotonic() - self.start,
                "rss_bytes": proc.memory_info().rss,
                "system_available_bytes": psutil.virtual_memory().available,
            }
            try:
                line = (
                    subprocess.check_output(
                        [
                            "nvidia-smi",
                            "--query-gpu=memory.used,utilization.gpu",
                            "--format=csv,noheader,nounits",
                        ],
                        text=True,
                    )
                    .strip()
                    .splitlines()[0]
                )
                mem, util = line.split(",")
                sample.update(device_used_mib=int(mem), gpu_util_percent=int(util))
            except (OSError, subprocess.SubprocessError, ValueError):
                pass
            self.samples.append(sample)
            self.stop.wait(1)

    def __enter__(self):
        self.start = time.monotonic()
        self.thread = threading.Thread(target=self.sample, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, typ, exc, tb):
        self.stop.set()
        self.thread.join()
        save(
            self.path,
            {
                "wall_seconds": time.monotonic() - self.start,
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                * 1024,
                "sampled_peak_rss_bytes": max(
                    (s["rss_bytes"] for s in self.samples), default=0
                ),
                "sampled_peak_device_mib": max(
                    (s.get("device_used_mib", 0) for s in self.samples), default=0
                ),
                "error_type": typ.__name__ if typ else None,
                "error": str(exc) if exc else None,
                "sample_interval_seconds": 1,
                "samples": self.samples,
            },
        )


def select():
    from llm.event_pipeline.semantic.core import publication

    docs = rows("data/processed/events/news-corpus-v1/documents.jsonl")
    rng = random.Random(SEED)
    strata = defaultdict(list)
    source_docs = defaultdict(list)
    dates = []
    for d in sorted(docs, key=lambda d: d["document_id"]):
        pub = publication(d)
        dt = datetime.fromisoformat(pub["value"]) if pub["value"] else None
        week = "%04d-W%02d" % dt.isocalendar()[:2] if dt else "unknown"
        strata[d["article"]["source"], week].append(d["document_id"])
        source_docs[d["article"]["source"]].append(d["document_id"])
        if dt:
            dates.append(dt.date().isoformat())
    # sqrt source weighting limits the dominant exchange source; round-robin weeks.
    weights = {s: len(v) ** 0.5 for s, v in source_docs.items()}
    quotas = {
        s: min(len(source_docs[s]), int(1000 * w / sum(weights.values())))
        for s, w in weights.items()
    }
    while sum(quotas.values()) < 1000:
        s = max(
            (s for s in weights if quotas[s] < len(source_docs[s])),
            key=lambda s: (1000 * weights[s] / sum(weights.values()) - quotas[s], s),
        )
        quotas[s] += 1
    chosen = []
    for s in sorted(quotas):
        buckets = [list(strata[s, w]) for ss, w in sorted(strata) if ss == s]
        for b in buckets:
            rng.shuffle(b)
        for _ in range(quotas[s]):
            candidates = [b for b in buckets if b]
            bucket = candidates[_ % len(candidates)]
            chosen.append(bucket.pop())
    chosen.sort()
    save(ROOT / "selection.json", chosen)
    counts = Counter(d["text_sha256"] for d in docs)
    save(
        ROOT / "selection-audit.json",
        {
            "seed": SEED,
            "algorithm": "sqrt source allocation with largest deficits; seeded shuffled source/week round-robin; no vector or threshold access",
            "corpus_documents": len(docs),
            "selected_documents": len(chosen),
            "corpus_sources": dict(Counter(d["article"]["source"] for d in docs)),
            "selected_sources": quotas,
            "source_week_counts": {
                s + "|" + w: len(v) for (s, w), v in sorted(strata.items())
            },
            "date_min": min(dates),
            "date_max": max(dates),
            "unknown_dates": len(docs) - len(dates),
            "text_chars": quantiles([len(d["article"]["text"]) for d in docs]),
            "duplicate_text_families": sum(c > 1 for c in counts.values()),
            "duplicate_excess_documents": sum(c - 1 for c in counts.values()),
            "documents_sha256": hashlib.sha256(
                Path(
                    "data/processed/events/news-corpus-v1/documents.jsonl"
                ).read_bytes()
            ).hexdigest(),
        },
    )
    print(json.dumps({"sources": quotas, "selection": len(chosen)}))


def download():
    from huggingface_hub import snapshot_download

    with Measure(ROOT / "metrics/download.json"):
        path = snapshot_download(
            "BAAI/bge-m3",
            revision=REVISION,
            allow_patterns=[
                "*.json",
                "pytorch_model.bin",
                "sentencepiece.bpe.model",
                "1_Pooling/*",
            ],
            ignore_patterns=["onnx/*", "imgs/*"],
        )
    files = {
        str(p.relative_to(path)): {
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in Path(path).rglob("*")
        if p.is_file()
    }
    save(
        ROOT / "model-download.json",
        {
            "revision": REVISION,
            "files": files,
            "total_bytes": sum(f["bytes"] for f in files.values()),
        },
    )


def embedding(args):
    import torch
    from llm.event_pipeline.semantic.encoder import SentenceEncoder
    from llm.event_pipeline.semantic.experiment import embed

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA required")
    torch.set_num_threads(4)
    with Measure(ROOT / f"metrics/{args.label}.json"):
        start = time.monotonic()
        encoder = SentenceEncoder(
            revision=REVISION, device="cuda", allow_download=False
        )
        torch.cuda.synchronize()
        load_seconds = time.monotonic() - start
        torch.cuda.reset_peak_memory_stats()
        start = time.monotonic()
        original_encode = encoder.encode
        completed_batches = 0

        def progress(texts):
            nonlocal completed_batches
            vectors, counts = original_encode(texts)
            completed_batches += 1
            if completed_batches == 1 or completed_batches % 10 == 0:
                print(
                    json.dumps(
                        {
                            "batches_this_invocation": completed_batches,
                            "elapsed_seconds": time.monotonic() - start,
                        }
                    ),
                    flush=True,
                )
            return vectors, counts

        encoder.encode = progress
        result = embed(
            args.run, encoder, batch_size=args.batch_size, max_batches=args.max_batches
        )
        torch.cuda.synchronize()
        result.update(
            load_seconds=load_seconds,
            inference_wall_seconds=time.monotonic() - start,
            peak_cuda_allocated_bytes=torch.cuda.max_memory_allocated(),
            peak_cuda_reserved_bytes=torch.cuda.max_memory_reserved(),
            dtype=str(next(encoder.model.parameters()).dtype),
        )
        save(ROOT / f"metrics/{args.label}-result.json", result)
        print(json.dumps(result), flush=True)


def comparison(args):
    from llm.event_pipeline.semantic.experiment import compare

    with Measure(ROOT / f"metrics/{args.label}.json"):
        result = compare(
            args.run,
            ROOT / "comparisons" / args.label,
            threshold=args.threshold,
            alpha=args.alpha,
            half_life_days=args.half_life,
            backend="scipy",
            max_units=args.max_units,
            max_matrix_mb=args.max_matrix_mb,
            neighbors=10,
        )
    print(json.dumps(result), flush=True)


def expand_cache():
    import sqlite3
    import shutil

    source = ROOT / "run"
    destination = ROOT / "full-run"
    if (destination / "encoder.json").exists() or (
        destination / "vectors.sqlite3"
    ).exists():
        raise ValueError("full-run cache already exists")
    with (
        sqlite3.connect(source / "vectors.sqlite3") as src,
        sqlite3.connect(destination / "vectors.sqlite3") as dst,
    ):
        src.backup(dst)
    shutil.copyfile(source / "encoder.json", destination / "encoder.json")
    save(
        destination / "cache-import.json",
        {
            "source_plan_sha256": hashlib.sha256(
                (source / "plan.json").read_bytes()
            ).hexdigest(),
            "source_cache_sha256": hashlib.sha256(
                (source / "vectors.sqlite3").read_bytes()
            ).hexdigest(),
            "policy": "Reuse identical text/profile keys and cumulative batch measurements; embedding recomputes only absent keys.",
        },
    )


def environment():
    import torch
    import psutil

    save(
        ROOT / "environment.json",
        {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu": subprocess.check_output(["lscpu"], text=True),
            "ram_bytes": psutil.virtual_memory().total,
            "torch": torch.__version__,
            "cuda": torch.version.cuda,
            "cuda_available": torch.cuda.is_available(),
            "gpu": torch.cuda.get_device_name(),
            "gpu_capacity_bytes": torch.cuda.get_device_properties(0).total_memory,
            "nvidia_smi": subprocess.check_output(["nvidia-smi"], text=True),
            "parent_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "HF_HOME": os.environ.get("HF_HOME"),
            "seed": SEED,
        },
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "command",
        choices=[
            "select",
            "download",
            "embed",
            "environment",
            "compare",
            "expand-cache",
        ],
    )
    p.add_argument("--run", default=str(ROOT / "run"))
    p.add_argument("--label", default="embed")
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--max-batches", type=int, default=1000)
    p.add_argument("--threshold", type=float, default=0.75)
    p.add_argument("--alpha", type=float, default=0.4)
    p.add_argument("--half-life", type=float, default=7)
    p.add_argument("--max-units", type=int, default=5000)
    p.add_argument("--max-matrix-mb", type=int, default=512)
    args = p.parse_args()
    if args.command == "select":
        select()
    elif args.command == "download":
        download()
    elif args.command == "environment":
        environment()
    elif args.command == "compare":
        comparison(args)
    elif args.command == "expand-cache":
        expand_cache()
    else:
        embedding(args)
