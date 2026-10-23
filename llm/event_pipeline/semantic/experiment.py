"""Immutable corpus plans, restartable vector cache and atomic experiment exports."""

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from ..blocks import source_blocks
from .core import VERSION, canonical, digest, publication, group_vectors, normalize


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).write_text(canonical(value), encoding="utf-8")


@contextmanager
def atomic_directory(destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("output_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".semantic-", dir=destination.parent))
    try:
        yield temporary
        if destination.exists():
            raise ValueError("output_exists")
        os.rename(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


@contextmanager
def locked(root):
    with (Path(root) / "experiment.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("experiment_busy") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def make_units(document, max_chars):
    blocks = source_blocks(document, max_chars)
    body = document["article"]["text"]
    title = document["article"]["title"]
    title_allowed = not any(
        p.get("title_origin") == "generated_identifier"
        for p in document.get("provenance", [])
    )
    if body.strip():
        if len(body) <= max_chars:
            spans = [("text", 0, len(body))]
        else:
            spans = [(b.field, b.start, b.end) for b in blocks if b.field == "text"]
            # Pack adjacent paragraphs without changing intervening characters.
            packed = []
            for field, start, end in spans:
                if packed and end - packed[-1][1] <= max_chars:
                    packed[-1] = (field, packed[-1][1], end)
                else:
                    packed.append((field, start, end))
            spans = packed
    else:
        spans = [(b.field, b.start, b.end) for b in blocks if b.field == "title"]
    units = []
    for field, start, end in spans:
        text = document["article"][field][start:end]
        # The title is context, not an independently discovered event.
        embedding_text = (
            title + "\n\n" if title_allowed and title and field != "title" else ""
        ) + text
        identity = [
            document["document_id"],
            document["article_sha256"],
            field,
            start,
            end,
        ]
        units.append(
            {
                "unit_id": digest(identity),
                "document_id": document["document_id"],
                "field": field,
                "start": start,
                "end": end,
                "text": text,
                "embedding_text": embedding_text,
                "text_hash": digest(embedding_text),
                "publication": publication(document),
            }
        )
    return units


def plan(corpus, output, *, document_ids=None, max_chars=2000):
    if type(max_chars) is not int or max_chars < 1:
        raise ValueError("invalid_max_chars")
    corpus = Path(corpus)
    manifest = read(corpus / "manifest.json")
    if manifest["schema_version"] != "prepared-news.v1":
        raise ValueError("unsupported_corpus")
    raw = (corpus / "documents.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["outputs"]["documents.jsonl"]:
        raise ValueError("corpus_hash_mismatch")
    documents = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if len({d["document_id"] for d in documents}) != len(documents):
        raise ValueError("duplicate_document")
    from llm.events.contracts.models import ArticleInput
    from llm.events.contracts.validation import article_sha256

    for doc in documents:
        if (
            article_sha256(ArticleInput.model_validate(doc["article"]))
            != doc["article_sha256"]
        ):
            raise ValueError("article_hash_mismatch")
        if (
            hashlib.sha256(doc["article"]["text"].encode()).hexdigest()
            != doc["text_sha256"]
        ):
            raise ValueError("text_hash_mismatch")
    if document_ids is not None:
        if not isinstance(document_ids, list) or any(
            not isinstance(x, str) for x in document_ids
        ):
            raise ValueError("invalid_document_selection")
        if len(set(document_ids)) != len(document_ids):
            raise ValueError("duplicate_selection")
        if set(document_ids) - {d["document_id"] for d in documents}:
            raise ValueError("unknown_document")
        documents = [d for d in documents if d["document_id"] in set(document_ids)]
    documents.sort(key=lambda d: d["document_id"])
    if len({d["article"]["article_id"] for d in documents}) != len(documents):
        raise ValueError("select_one_revision")
    units = sorted(
        (u for d in documents for u in make_units(d, max_chars)),
        key=lambda u: u["unit_id"],
    )
    value = {
        "schema_version": VERSION,
        "corpus_id": manifest["corpus_id"],
        "max_chars": max_chars,
        "documents": documents,
        "units": units,
        "documents_without_units": [
            d["document_id"]
            for d in documents
            if not any(u["document_id"] == d["document_id"] for u in units)
        ],
    }
    value["run_id"] = digest(value)
    with atomic_directory(output) as root:
        write(root / "plan.json", value)
    return {"run_id": value["run_id"], "documents": len(documents), "units": len(units)}


def load_plan(root):
    value = read(Path(root) / "plan.json")
    if value["schema_version"] != VERSION or value["run_id"] != digest(
        {k: v for k, v in value.items() if k != "run_id"}
    ):
        raise ValueError("plan_hash_mismatch")
    return value


def cache(root):
    db = sqlite3.connect(Path(root) / "vectors.sqlite3", timeout=5)
    db.execute("PRAGMA synchronous=FULL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS vectors(key TEXT PRIMARY KEY, vector TEXT NOT NULL, tokens INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY, profile TEXT NOT NULL, texts INTEGER NOT NULL, tokens INTEGER NOT NULL, seconds REAL NOT NULL)"
    )
    return db


def embed(root, encoder, *, batch_size=16, max_batches=100):
    """Encoder has a pinned profile and encode(texts)->(vectors, token_counts)."""
    if (
        type(batch_size) is not int
        or batch_size < 1
        or type(max_batches) is not int
        or max_batches < 1
    ):
        raise ValueError("invalid_batch_limit")
    with locked(root):
        value = load_plan(root)
        profile = encoder.profile
        profile_path = Path(root) / "encoder.json"
        if profile_path.exists() and read(profile_path) != profile:
            raise ValueError("encoder_changed_create_new_run")
        if not profile_path.exists():
            temporary = Path(root) / ".encoder.tmp"
            write(temporary, profile)
            os.replace(temporary, profile_path)
        db = cache(root)
        try:
            unique = {u["text_hash"]: u["embedding_text"] for u in value["units"]}
            pending = [
                (digest([profile, key]), text)
                for key, text in sorted(unique.items())
                if not db.execute(
                    "SELECT 1 FROM vectors WHERE key=?", (digest([profile, key]),)
                ).fetchone()
            ]
            completed = 0
            for start in range(
                0, min(len(pending), max_batches * batch_size), batch_size
            ):
                batch = pending[start : start + batch_size]
                before = time.monotonic()
                vectors, tokens = encoder.encode([text for _, text in batch])
                if len(vectors) != len(batch) or len(tokens) != len(batch):
                    raise ValueError("embedding_count_mismatch")
                vectors = [normalize(v) for v in vectors]
                existing = db.execute("SELECT vector FROM vectors LIMIT 1").fetchone()
                dimensions = {len(v) for v in vectors}
                if existing:
                    dimensions.add(len(json.loads(existing[0])))
                if len(dimensions) != 1 or any(
                    type(n) is not int or n < 0 for n in tokens
                ):
                    raise ValueError("invalid_embedding_shape_or_usage")
                with db:
                    db.executemany(
                        "INSERT INTO vectors VALUES (?,?,?)",
                        [
                            (key, canonical(v), n)
                            for (key, _), v, n in zip(
                                batch, vectors, tokens, strict=True
                            )
                        ],
                    )
                    db.execute(
                        "INSERT INTO batches(profile,texts,tokens,seconds) VALUES (?,?,?,?)",
                        (
                            digest(profile),
                            len(batch),
                            sum(tokens),
                            time.monotonic() - before,
                        ),
                    )
                completed += len(batch)
            return {
                "new_vectors": completed,
                "remaining": len(pending) - completed,
                "complete": len(pending) == completed,
                "unique_texts": len(unique),
            }
        finally:
            db.close()


def compare(
    root,
    output,
    *,
    threshold=0.75,
    alpha=0.4,
    half_life_days=7,
    backend="scipy",
    max_units=5000,
    max_matrix_mb=512,
    neighbors=10,
):
    with locked(root):
        value = load_plan(root)
        profile = read(Path(root) / "encoder.json")
        db = cache(root)
        try:
            vectors = []
            for unit in value["units"]:
                row = db.execute(
                    "SELECT vector FROM vectors WHERE key=?",
                    (digest([profile, unit["text_hash"]]),),
                ).fetchone()
                if row is None:
                    raise ValueError("embeddings_incomplete")
                vectors.append(json.loads(row[0]))
            usage = {"batches": 0, "encoded_texts": 0, "input_tokens": 0, "seconds": 0}
            for texts, tokens, seconds in db.execute(
                "SELECT texts,tokens,seconds FROM batches"
            ):
                usage["batches"] += 1
                usage["encoded_texts"] += texts
                usage["input_tokens"] += tokens
                usage["seconds"] += seconds
        finally:
            db.close()
        configuration = dict(
            threshold=threshold,
            half_life_days=half_life_days,
            backend=backend,
            max_units=max_units,
            max_matrix_mb=max_matrix_mb,
            neighbors=neighbors,
        )
        results = {}
        for name, weight in (("semantic", 0.0), ("semantic_time", alpha)):
            start = time.monotonic()
            result = group_vectors(
                vectors,
                [u["publication"] for u in value["units"]],
                alpha=weight,
                **configuration,
            )
            groups = []
            for indices, minimum in zip(
                result["groups"], result["minimum_scores"], strict=True
            ):
                ids = [value["units"][i]["unit_id"] for i in indices]
                groups.append(
                    {
                        "group_id": digest(
                            [value["run_id"], name, configuration, weight, ids]
                        ),
                        "unit_ids": ids,
                        "minimum_pair_score": minimum,
                        "interpretation": "reading_group_not_event_identity",
                    }
                )
            edges = [
                {
                    **edge,
                    "left": value["units"][edge["left"]]["unit_id"],
                    "right": value["units"][edge["right"]]["unit_id"],
                }
                for edge in result["neighbors"]
            ]
            results[name] = {
                "groups": groups,
                "neighbors": edges,
                "alpha": weight,
                "seconds": time.monotonic() - start,
            }
        with atomic_directory(output) as destination:

            def jsonl(name, rows):
                (destination / name).write_text(
                    "".join(canonical(row) + "\n" for row in rows), encoding="utf-8"
                )

            jsonl("documents.jsonl", value["documents"])
            jsonl("units.jsonl", value["units"])
            jsonl(
                "vectors.jsonl",
                [
                    {"unit_id": u["unit_id"], "vector": normalize(v)}
                    for u, v in zip(value["units"], vectors, strict=True)
                ],
            )
            for name, result in results.items():
                jsonl(name + ".groups.jsonl", result["groups"])
                jsonl(name + ".neighbors.jsonl", result["neighbors"])
            report = {
                "usage": usage,
                "documents": len(value["documents"]),
                "units": len(vectors),
                "documents_without_units": value["documents_without_units"],
                "variants": {
                    name: {
                        "groups": len(r["groups"]),
                        "singletons": sum(len(g["unit_ids"]) == 1 for g in r["groups"]),
                        "seconds": r["seconds"],
                        "alpha": r["alpha"],
                    }
                    for name, r in results.items()
                },
                "limitations": [
                    "Группы не подтверждают идентичность событий.",
                    "Качество без ручного эталона не измерено.",
                    "Время публикации не является временем события.",
                ],
            }
            write(destination / "report.json", report)
            import importlib.metadata

            packages = {}
            for package in ("numpy", "scipy"):
                try:
                    packages[package] = importlib.metadata.version(package)
                except importlib.metadata.PackageNotFoundError:
                    pass
            write(
                destination / "manifest.json",
                {
                    "schema_version": VERSION,
                    "run_id": value["run_id"],
                    "encoder": profile,
                    "configuration": {**configuration, "alpha": alpha},
                    "packages": packages,
                    "implementation": {
                        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in Path(__file__).parent.glob("*.py")
                    },
                    "outputs": {
                        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in destination.iterdir()
                    },
                },
            )
        return report
