"""Pinned BGE CPU encoding admitted to a stored space only by hash + numerical parity."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import sqlite3
import time
from pathlib import Path

import numpy as np

from .evaluation import atomic_json
from .models import canonical, digest, text_hash


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_parity(observed, reference, *, minimum_cosine=0.99999, maximum_error=1e-4):
    observed = np.asarray(observed)
    reference = np.asarray(reference)
    if observed.shape != reference.shape or observed.ndim != 2 or not len(observed):
        raise ValueError("parity_shape_mismatch")
    if not np.isfinite(observed).all() or not np.isfinite(reference).all():
        raise ValueError("parity_nonfinite")
    norms = np.linalg.norm(observed, axis=1) * np.linalg.norm(reference, axis=1)
    if (norms == 0).any():
        raise ValueError("parity_zero_vector")
    cosines = np.sum(observed.astype(np.float64) * reference, axis=1) / norms
    maximum = float(np.max(np.abs(observed - reference)))
    report = {
        "samples": len(observed),
        "minimum_cosine": float(cosines.min()),
        "maximum_absolute_error": maximum,
        "minimum_cosine_required": minimum_cosine,
        "maximum_error_allowed": maximum_error,
    }
    if report["minimum_cosine"] < minimum_cosine or maximum > maximum_error:
        raise ValueError("cpu_gpu_parity_failed:" + canonical(report))
    return report


class CPUEncoder:
    """fingerprint is a verified compatibility alias, runtime_fingerprint records CPU identity."""

    def __init__(self, news_root, model_path, output, *, threads=2, batch_size=4):
        news_root = Path(news_root)
        model_path = Path(model_path)
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        self.metadata = json.loads((news_root / "bge-index.json").read_text())
        expected = self.metadata["profile"]
        if digest(expected) != self.metadata["fingerprint"]:
            raise ValueError("corrupt_reference_profile")
        p = expected["profile"]
        if (
            p["model"] != "BAAI/bge-m3"
            or p["revision"] != "5617a9f61b028005a4858fdac845db406aefb181"
            or expected["precision"] != "fp32"
            or expected["prefix"] != ""
            or expected["text_policy"] != "exact-utf8.v1"
            or expected["dimension"] != 1024
        ):
            raise ValueError("unsupported_reference_space")
        for name, h in expected["files"].items():
            path = (model_path / name).resolve()
            if (
                Path(name).is_absolute()
                or ".." in Path(name).parts
                or file_hash(path) != h
            ):
                raise ValueError("model_file_mismatch:" + name)
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(threads)
        self.model = SentenceTransformer(
            str(model_path),
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
            prompts={"query": "", "document": ""},
        )
        self.model.float().eval()
        if self.model[1].get_config_dict() != expected["pooling"]:
            raise ValueError("pooling_profile_mismatch")
        if self.model.max_seq_length != p["max_tokens"]:
            raise ValueError("model_context_mismatch")
        self.dimension = 1024
        self.batch_size = batch_size
        self.runtime = {
            "adapter": "bge-cpu-verified.v1",
            "reference_profile": expected,
            "device": "cpu",
            "packages": {
                k: importlib.metadata.version(k)
                for k in ("torch", "sentence-transformers", "transformers")
            },
            "model_files": expected["files"],
            "threads": threads,
            "batch_size": batch_size,
        }
        self.runtime_fingerprint = digest(self.runtime)
        self._path = output / "event-vectors.sqlite3"
        with sqlite3.connect(self._path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY,payload BLOB NOT NULL)"
            )
        # Both query and document parity: no inference on the full source corpus.
        queries = self.metadata["query_texts"][:8]
        qref = np.load(news_root / "bge-queries.npy", allow_pickle=False)[
            : len(queries)
        ]
        docs = {
            d["reference"]["document_id"]: d
            for d in map(
                json.loads, (news_root / "documents.jsonl").read_text().splitlines()
            )
        }
        positions = np.linspace(0, len(self.metadata["document_ids"]) - 1, 8, dtype=int)
        texts = []
        for pos in positions:
            d = docs[self.metadata["document_ids"][int(pos)]]
            texts.append(
                (
                    d["title"] + "\n\n"
                    if d["title_origin"] == "source" and d["title"]
                    else ""
                )
                + d["text"]
            )
        dref = np.load(news_root / "bge-documents.npy", allow_pickle=False)[positions]
        start = time.monotonic()
        report = {
            "status": "verified",
            "reference_fingerprint": self.metadata["fingerprint"],
            "runtime_fingerprint": self.runtime_fingerprint,
            "runtime": self.runtime,
            "query_parity": check_parity(self._encode(queries), qref),
            "document_parity": check_parity(self._encode(texts), dref),
            "seconds": time.monotonic() - start,
            "limitation": "Numerical compatibility demonstrated on fixed samples; runtime identity is not CUDA identity.",
        }
        atomic_json(output / "cpu-compatibility.json", report)
        self.compatibility = report
        # Only expose the alias after both numerical checks and all weight hashes passed.
        self.fingerprint = self.metadata["fingerprint"]
        self.metrics = {"cache_hits": 0, "encoded": 0, "seconds": 0.0}

    def _encode(self, texts):
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        lengths = [
            len(self.model.tokenizer(t, truncation=False)["input_ids"]) for t in texts
        ]
        if any(n > self.model.max_seq_length for n in lengths):
            raise ValueError("event_input_too_long_no_truncation")
        values = self.model.encode(
            texts,
            prompt="",
            batch_size=self.batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        values = values.astype(np.float64)
        return (values / np.linalg.norm(values, axis=1, keepdims=True)).astype(
            np.float32
        )

    def encode_event_texts(self, texts):
        output = {}
        pending = []
        with sqlite3.connect(self._path) as db:
            for text in dict.fromkeys(texts):
                key = digest([self.runtime_fingerprint, text_hash(text)])
                row = db.execute(
                    "SELECT payload FROM vectors WHERE key=?", (key,)
                ).fetchone()
                if row:
                    value = np.frombuffer(row[0], dtype="<f4").copy()
                    if (
                        value.shape != (self.dimension,)
                        or not np.isfinite(value).all()
                        or abs(np.linalg.norm(value) - 1) > 1e-5
                    ):
                        raise ValueError("corrupt_event_vector_cache")
                    output[text] = value
                    self.metrics["cache_hits"] += 1
                else:
                    pending.append(text)
            if pending:
                start = time.monotonic()
                vectors = self._encode(pending)
                self.metrics["seconds"] += time.monotonic() - start
                self.metrics["encoded"] += len(pending)
                for text, vector in zip(pending, vectors):
                    db.execute(
                        "INSERT OR IGNORE INTO vectors VALUES (?,?)",
                        (
                            digest([self.runtime_fingerprint, text_hash(text)]),
                            vector.astype("<f4").tobytes(),
                        ),
                    )
                    output[text] = vector
        return (
            np.stack([output[t] for t in texts])
            if texts
            else np.empty((0, self.dimension), np.float32)
        )
