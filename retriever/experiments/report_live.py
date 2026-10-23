"""Standalone offline team explorer for originals, live decisions and CPU event vectors."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE
from threadpoolctl import threadpool_limits

from llm.cluster_merge.core import parse_answer
from llm.cluster_merge.models import Fragment
from retriever.evaluation import atomic_json
from retriever.models import digest


def rows(path):
    return [json.loads(s) for s in Path(path).read_text().splitlines() if s.strip()]


def read(path):
    return json.loads(Path(path).read_text())


def build(root, run):
    news = root / "data/processed/company_news/run-20261023"
    meta = read(news / "bge-index.json")
    documents = rows(news / "documents.jsonl")
    vectors = np.load(news / "bge-documents.npy", allow_pickle=False)
    with sqlite3.connect(
        (run / "index.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
    ) as db:
        items = [
            dict(json.loads(p), active=bool(a))
            for p, a in db.execute("SELECT payload,active FROM items")
        ]
        vecs = {
            i: json.loads(p)["values"]
            for i, p in db.execute("SELECT item_id,payload FROM vectors")
        }
        groups = [json.loads(r[0]) for r in db.execute("SELECT payload FROM clusters")]
    events = [i for i in items if i["kind"] == "event"]
    with sqlite3.connect(
        (run / "merge.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
    ) as db:
        operations = [
            {"id": i, "request": json.loads(r), "state": json.loads(s)}
            for i, r, s in db.execute("SELECT id,request,state FROM operations")
        ]
        calls = [
            {
                "id": i,
                "operation": op,
                "status": s,
                "request": json.loads(q),
                "response": json.loads(r) if r else None,
            }
            for i, op, s, q, r in db.execute(
                "SELECT id,operation,status,request,response FROM calls"
            )
        ]
        results = [json.loads(p) for (p,) in db.execute("SELECT payload FROM results")]
    for call in calls:
        call["structural_error"] = None
        if call["response"]:
            try:
                choice = call["response"]["choices"][0]
                if choice["finish_reason"] != "stop":
                    raise ValueError("finish_reason:" + str(choice["finish_reason"]))
                fs = [
                    Fragment.model_validate(f)
                    for f in json.loads(call["request"][1]["content"])["fragments"]
                ]
                parse_answer(choice["message"]["content"], fs)
            except (ValueError, KeyError, TypeError, IndexError) as exc:
                call["structural_error"] = str(exc)[:1500]
    profiles = []
    query_points = []
    query_vectors = []
    for path in sorted(run.glob("U_*-profile.json")):
        p = read(path)
        interests = read(run / (p["company_id"] + "-interests.json"))
        final = (
            read(run / p["company_id"] / "batch.json")
            if (run / p["company_id"] / "batch.json").exists()
            else None
        )
        trace = read(run / p["company_id"] / "trace.json") if final else None
        basepath = (
            root
            / "data/processed/retriever/retrieval-pilot-20261024"
            / p["company_id"]
            / "baseline/batch.json"
        )
        baseline = read(basepath) if basepath.exists() else None
        profiles.append(
            {
                "profile": p,
                "interests": interests,
                "batch": final,
                "trace": trace,
                "baseline": baseline,
                "first_pass": read(
                    run / "history" / "first-pass" / p["company_id"] / "batch.json"
                )
                if (
                    run / "history" / "first-pass" / p["company_id"] / "batch.json"
                ).exists()
                else None,
            }
        )
        for q in interests["interests"]:
            query_points.append(
                {
                    "id": "query:" + p["company_id"] + ":" + q["interest_id"],
                    "kind": "interest",
                    "company": p["company_id"],
                    "title": q["text"],
                    "text": q["text"],
                }
            )
            query_vectors.append(q["vector"])
    active = {i["item_id"] for i in items if i["active"]}
    points = [
        {
            "id": d["reference"]["document_id"],
            "kind": "article",
            "title": d["title"],
            "text": d["text"],
            "title_origin": d["title_origin"],
            "reference": d["reference"],
            "provenance": d["provenance"],
            "active": d["reference"]["document_id"] in active,
        }
        for d in documents
    ]
    points += [
        {
            "id": e["item_id"],
            "kind": "event",
            "title": e["title"],
            "text": e["text"],
            "event": e["derived_from"]["event"],
            "source_ids": e["source_revision_ids"],
            "active": e["active"],
        }
        for e in events
    ]
    points += query_points
    combined = np.vstack(
        [vectors]
        + (
            [np.array([vecs[e["item_id"]] for e in events], np.float32)]
            if events
            else []
        )
        + ([np.array(query_vectors, np.float32)] if query_vectors else [])
    )
    identity = digest([meta["fingerprint"], [p["id"] for p in points]])
    saved = run / "projection.json"
    if saved.exists() and read(saved).get("identity") == identity:
        projection = read(saved)
    else:
        with threadpool_limits(limits=2):
            # Fit PCA to originals; transform all extra points without moving the original map.
            pca = PCA(n_components=2, svd_solver="randomized", random_state=42).fit(
                vectors
            )
            xy = pca.transform(combined)
            tsne = TSNE(
                n_components=2,
                metric="cosine",
                init="pca",
                perplexity=40,
                max_iter=750,
                random_state=42,
                learning_rate="auto",
                n_jobs=2,
            ).fit_transform(combined)
        projection = {
            "identity": identity,
            "pca": xy.round(5).tolist(),
            "tsne": tsne.round(4).tolist(),
            "pca_variance": pca.explained_variance_ratio_.tolist(),
            "tsne_perplexity": 40,
            "tsne_seed": 42,
        }
        atomic_json(saved, projection)
    with threadpool_limits(limits=2):
        similarities = combined @ combined.T
    np.fill_diagonal(similarities, -np.inf)
    for i, p in enumerate(points):
        p["pca"] = projection["pca"][i]
        p["tsne"] = projection["tsne"][i]
        best = np.argsort(-similarities[i], kind="stable")[:10]
        p["neighbors"] = [
            {"id": points[int(j)]["id"], "cosine": round(float(similarities[i, j]), 5)}
            for j in best
        ]

    usage = [c["response"].get("usage", {}) for c in calls if c["response"]]
    plotted = {p["id"] for p in points}
    unapplied = [
        {
            "id": e["id"],
            "kind": "event",
            "title": e["title"],
            "text": e["text"],
            "event": e,
            "source_ids": e["article_ids"],
            "active": False,
        }
        for r in results
        for e in r["events"]
        if e["id"] not in plotted
    ]
    data = {
        "unapplied_events": unapplied,
        "points": points,
        "profiles": profiles,
        "groups": groups,
        "calls": calls,
        "operations": operations,
        "results": results,
        "compatibility": read(run / "cpu-compatibility.json"),
        "summary": read(run / "summary.json"),
        "projection": {k: v for k, v in projection.items() if k not in ("pca", "tsne")},
        "stats": {
            "documents": len(documents),
            "events": len(events),
            "active_events": sum(e["active"] for e in events),
            "calls": len(calls),
            "input_tokens": sum(u.get("prompt_tokens", 0) or 0 for u in usage),
            "output_tokens": sum(u.get("completion_tokens", 0) or 0 for u in usage),
        },
    }
    data["diagnostics"] = {
        "events_with_reference_ids_in_text": [
            e["item_id"]
            for e in events
            if any(
                f["id"] in e["text"] for f in e["derived_from"]["event"]["fragments"]
            )
        ],
        "structurally_invalid_responses": sum(
            bool(c.get("structural_error")) for c in calls
        ),
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
    template = Path(__file__).with_name("report_live.html").read_text()
    temporary = run / "report.html.tmp"
    temporary.write_text(template.replace("__DATA__", payload), encoding="utf-8")
    temporary.replace(run / "report.html")
    print(
        json.dumps(
            {"report": str(run / "report.html"), "stats": data["stats"]},
            ensure_ascii=False,
        )
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--run", type=Path, required=True)
    a = p.parse_args()
    build(a.root, a.run)


if __name__ == "__main__":
    main()
