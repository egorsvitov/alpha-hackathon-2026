"""Real saved-vector retrieval and bounded live lazy merge. No hidden retry/repair."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

import numpy as np

from llm.cluster_merge import MergeService, OpenAIProvider, Profile
from retriever.cpu_encoder import CPUEncoder, file_hash
from retriever.evaluation import atomic_json, make_review_pool
from retriever.merge_adapter import ClusterMergeAdapter
from retriever.models import (
    ArticleRevision,
    ClusterSnapshot,
    CompanyInterestSet,
    Interest,
    RetrievalRequest,
    SearchItem,
    VectorRecord,
    text_hash,
)
from retriever.output import write_run
from retriever.selection import RetrieverService
from retriever.store import SQLiteSearchStore


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(s) for s in Path(path).read_text().splitlines() if s.strip()]


class AuditReservations:
    """User-authorized credit experiment: journal reservations without monetary gating."""

    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY,created REAL NOT NULL)"
            )

    def reserve(self, key):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO reservations VALUES (?,?)", (key, time.time()))


def run(root, output, model_path, companies, *, k, max_calls):
    news = root / "data/processed/company_news/run-20261023"
    interests_root = (
        root / "data/processed/retriever/activity-interests-v1/embeddings-bge-m3"
    )
    output.mkdir(parents=True, exist_ok=True)
    index = read(news / "bge-index.json")
    meta = read(interests_root / "manifest.json")
    if file_hash(news / "bge-index.json") != meta["identity"]["news_index_sha256"]:
        raise ValueError("news_interest_reference_mismatch")
    for name, h in meta["files"].items():
        if file_hash(interests_root / name) != h:
            raise ValueError("interest_artifact_hash_mismatch:" + name)
    wanted = [
        "documents.jsonl",
        "profiles.jsonl",
        "groups.jsonl",
        "bge-index.json",
        "bge-documents.npy",
        "bge-queries.npy",
    ]
    for line in (news / "ARTIFACTS.sha256").read_text().splitlines():
        h, name = line.split(None, 1)
        if name in wanted and file_hash(news / name) != h:
            raise ValueError("news_artifact_hash_mismatch:" + name)
    print(json.dumps({"stage": "cpu_encoder_loading"}), flush=True)
    encoder = CPUEncoder(news, model_path, output)
    print(
        json.dumps(
            {
                "stage": "cpu_parity_verified",
                "query": encoder.compatibility["query_parity"],
                "document": encoder.compatibility["document_parity"],
            }
        ),
        flush=True,
    )
    documents = rows(news / "documents.jsonl")
    matrix = np.load(news / "bge-documents.npy", allow_pickle=False)
    if [d["reference"]["document_id"] for d in documents] != index["document_ids"]:
        raise ValueError("document_vector_mapping_mismatch")
    if matrix.dtype != np.float32 or matrix.shape != (len(documents), 1024):
        raise ValueError("news_matrix_mismatch")
    articles = []
    items = []
    vectors = []
    for d, v in zip(documents, matrix):
        r = d["reference"]
        uid = r["document_id"]
        a = ArticleRevision(
            revision_id=uid,
            article_id=r["article_id"],
            text=d["text"],
            text_sha256=r["content_hash"],
            title=d["title"],
            title_origin=d["title_origin"],
            source=r["source"],
            published_at=r["published_at"],
            publication_precision=r["publication_precision"],
            observed_at=r.get("observed_at"),
            provenance=[
                {"kind": "gpu_experiment", "reference": r, "original": d["provenance"]}
            ],
        )
        text = (
            a.title + "\n\n" if a.title_origin == "source" and a.title else ""
        ) + a.text
        articles.append(a)
        items.append(
            SearchItem(
                item_id=uid,
                title=a.title,
                text=a.text,
                embedding_text=text,
                source_revision_ids=[uid],
            )
        )
        vectors.append(
            VectorRecord(
                vector_id=uid,
                item_id=uid,
                embedding_text_sha256=text_hash(text),
                values=v.tolist(),
            )
        )
    known = {a.revision_id for a in articles}
    groups = []
    missing = []
    for g in rows(news / "groups.jsonl"):
        members = set(g["document_ids"])
        present = sorted(members & known)
        if not present:
            continue
        absent = sorted(members - known)
        if absent:
            missing.append(
                {"group_id": g["group_id"], "missing": absent, "known": present}
            )
        groups.append(
            ClusterSnapshot(
                cluster_id=g["group_id"],
                revision="gpu-20261023",
                member_revision_ids=present,
                provenance={
                    "interpretation": g["interpretation"],
                    "incomplete_membership": bool(absent),
                    "missing_revision_ids": absent,
                },
            )
        )
    atomic_json(
        output / "import-audit.json",
        {
            "documents": len(articles),
            "groups": len(groups),
            "incomplete_groups": missing,
        },
    )
    topics = read(interests_root / "interests.json")
    qmatrix = np.load(interests_root / "interest-vectors.npy", allow_pickle=False)
    profiles = {p["company_id"]: p for p in rows(news / "profiles.jsonl")}
    journal = AuditReservations(output / "reservations.sqlite3")
    merge = ClusterMergeAdapter(
        MergeService(OpenAIProvider(), journal, output / "merge.sqlite3"), Profile()
    )
    summary = []
    paths = []
    corpus = "live-cpu-short:" + file_hash(news / "documents.jsonl")
    bundle = {
        "articles": articles,
        "items": items,
        "vectors": vectors,
        "clusters": groups,
        "encoder_fingerprint": encoder.fingerprint,
        "dimension": 1024,
        "corpus_id": corpus,
    }
    began = time.monotonic()
    with SQLiteSearchStore(output / "index.sqlite3") as store:
        store.import_bundle(**bundle)
        for company in companies:
            p = profiles[company]
            code = p["okved"]["code"]
            selected = [
                t
                for t in topics
                if t["okved_code"] == code and t["applicable_when"] is None
            ]
            interests = CompanyInterestSet(
                company_id=company,
                profile_version=p["profile_version"],
                interest_set_version=meta["identity"]["catalogue_sha256"],
                encoder_fingerprint=encoder.fingerprint,
                dimension=1024,
                interests=[
                    Interest(
                        interest_id=t["interest_id"],
                        text=t["description"],
                        vector=qmatrix[t["row"]].tolist(),
                        provenance={
                            "kind": "activity_hypothesis",
                            "review_status": t["review_status"],
                        },
                    )
                    for t in selected
                ],
            )
            atomic_json(output / (company + "-profile.json"), p)
            atomic_json(output / (company + "-interests.json"), interests)
            request = RetrievalRequest(
                request_id=company + "-live",
                company_id=company,
                profile_version=p["profile_version"],
                corpus_id=corpus,
                as_of="2026-10-24T00:00:00+03:00",
                k=k,
                max_new_llm_calls=max_calls,
                max_merge_operations=50,
                max_rebuilds=100,
                deadline_seconds=1800.0,
            )
            destination = output / company
            if destination.exists():
                paths.append(destination / "batch.json")
                summary.append(read(destination / "experiment-summary.json"))
                continue
            print(
                json.dumps(
                    {
                        "stage": "retrieve",
                        "company": company,
                        "activity": p["okved"]["name"],
                    }
                ),
                flush=True,
            )
            start = time.monotonic()
            run = RetrieverService().retrieve(
                request, interests=interests, store=store, merge=merge, encoder=encoder
            )
            write_run(run, destination)
            paths.append(destination / "batch.json")
            stats = {
                "company": company,
                "activity": p["okved"]["name"],
                "returned": len(run.batch.candidates),
                "status": run.batch.status,
                "calls_new": run.trace["calls_new"],
                "seconds": time.monotonic() - start,
                "event_candidates": sum(
                    c.item.kind == "event" for c in run.batch.candidates
                ),
                "deferred_clusters": len(run.batch.deferred),
                "generation": run.batch.index_generation,
                "encoder_metrics": dict(encoder.metrics),
            }
            atomic_json(destination / "experiment-summary.json", stats)
            summary.append(stats)
            atomic_json(
                output / "summary.json",
                {
                    "companies": summary,
                    "encoder": encoder.metrics,
                    "seconds": time.monotonic() - began,
                },
            )
            print(
                json.dumps({"stage": "company_complete", **stats}, ensure_ascii=False),
                flush=True,
            )
    atomic_json(output / "review.json", make_review_pool(paths))
    print(
        json.dumps({"stage": "complete", "output": str(output)}, ensure_ascii=False),
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument(
        "--companies",
        nargs="+",
        default=["U_100013", "U_100001", "U_100000", "U_10", "U_100090", "U_101278"],
    )
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--max-calls", type=int, default=8)
    args = parser.parse_args()
    # Loading credentials is exclusive to this explicitly invoked live CLI.
    env = args.root / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("LLM_API_KEY="):
                os.environ.setdefault(
                    "LLM_API_KEY", line.split("=", 1)[1].strip().strip("\"'")
                )
    if not os.environ.get("LLM_API_KEY"):
        raise ValueError("api_key_missing")
    if args.max_calls < 1 or args.k < 1:
        raise ValueError("positive_limits_required")
    run(
        args.root,
        args.output,
        args.model_path,
        args.companies,
        k=args.k,
        max_calls=args.max_calls,
    )


if __name__ == "__main__":
    main()
