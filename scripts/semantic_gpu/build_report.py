"""Build an offline, source-bearing exploration report from a frozen experiment.

No model downloads, database reads or API calls. Output belongs in ignored data/.
"""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import time

import numpy as np
import sklearn
from sklearn.decomposition import PCA
from sklearn.manifold import TSNE, trustworthiness
from sklearn.metrics import pairwise_distances
from threadpoolctl import threadpool_limits

ASSETS = Path(__file__).with_name("report")
SEED = 20261023


def read(path):
    return json.loads(path.read_text())


def rows(path):
    with path.open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def projections(matrix, ids, out, vector_hash):
    fingerprint = {"schema": 2, "vectors_sha256": vector_hash, "seed": SEED,
                   "sklearn": sklearn.__version__, "pca_components": 50,
                   "perplexity": 40, "max_iter": 1200, "threads": 4}
    cache = out / "projection.json"
    if cache.exists() and read(cache)["fingerprint"] == fingerprint:
        return read(cache)
    started = time.perf_counter()
    with threadpool_limits(limits=4):
        pca = PCA(n_components=50, svd_solver="randomized", random_state=SEED)
        reduced = pca.fit_transform(matrix)
        tsne = TSNE(n_components=2, perplexity=40, init="pca", learning_rate="auto",
                    max_iter=1200, random_state=SEED, method="barnes_hut", n_jobs=4)
        nonlinear = tsne.fit_transform(reduced)
        coordinates = {"tsne": nonlinear, "pca": reduced[:, :2]}
        # Exact original-space cosine neighbors. Equal vectors have tied ranks;
        # the stable ordering is disclosed rather than jittering the source data.
        distance = pairwise_distances(matrix, metric="cosine")
        np.fill_diagonal(distance, np.inf)
        nearest = np.argsort(distance, axis=1, kind="stable")[:, :10]
        metrics = {}
        for name, xy in coordinates.items():
            d2 = pairwise_distances(xy)
            np.fill_diagonal(d2, np.inf)
            projected = np.argsort(d2, axis=1, kind="stable")[:, :10]
            recall = np.mean([len(set(a) & set(b)) / 10 for a, b in zip(nearest, projected)])
            metrics[name] = {"trustworthiness_k10": float(trustworthiness(
                matrix, xy, n_neighbors=10, metric="cosine")), "neighbor_recall_k10": float(recall)}
    result = {"fingerprint": fingerprint, "unit_ids": ids,
              "coordinates": {k: v.round(6).tolist() for k, v in coordinates.items()},
              "metrics": metrics, "pca_variance_2d": float(pca.explained_variance_ratio_[:2].sum()),
              "pca_variance_50d": float(pca.explained_variance_ratio_.sum()),
              "tsne_kl_divergence": float(tsne.kl_divergence_),
              "tsne_n_iter_index": int(tsne.n_iter_),
              "seconds": time.perf_counter() - started,
              "method": "PCA50 randomized seeded -> t-SNE Euclidean; PCA2 uses first two PCA50 components. Quality compared to original 1024D cosine, all units, k=10, self excluded; stable tie ranks, exact duplicate texts retained.",
              "references": ["https://scikit-learn.org/stable/modules/generated/sklearn.manifold.TSNE.html",
                             "https://scikit-learn.org/stable/modules/generated/sklearn.manifold.trustworthiness.html"]}
    dump(cache, result)
    return result


def build(root, out, issues_file=None):
    if not out.resolve().is_relative_to((Path.cwd() / "data/processed/event_pipeline").resolve()):
        raise ValueError("Source-bearing output must stay in ignored data/processed/event_pipeline/")
    out.mkdir(parents=True, exist_ok=True)
    issues_file = issues_file or out / "issues.json"
    issues = read(issues_file)
    if issues_file.resolve() != (out / "issues.json").resolve():
        (out / "issues.json").write_bytes(issues_file.read_bytes())
    base = root / "comparisons/full-chosen"
    units = sorted(rows(base / "units.jsonl"), key=lambda u: u["unit_id"])
    ids = [u["unit_id"] for u in units]
    ix = {uid: i for i, uid in enumerate(ids)}
    vector_rows = rows(base / "vectors.jsonl")
    vectors = {r["unit_id"]: r["vector"] for r in vector_rows}
    assert set(vectors) == set(ids) and len(ids) == len(set(ids))
    matrix = np.asarray([vectors[uid] for uid in ids], dtype=np.float32)
    assert matrix.shape == (len(ids), 1024) and np.isfinite(matrix).all()
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-5)
    del vector_rows, vectors
    projection = projections(matrix, ids, out, digest(base / "vectors.jsonl"))
    assert projection["unit_ids"] == ids
    for xy in projection["coordinates"].values():
        assert np.asarray(xy).shape == (len(ids), 2) and np.isfinite(xy).all()
    documents = {d["document_id"]: d["article"] for d in rows(base / "documents.jsonl")}
    groups, memberships, neighbors = {}, {}, {}
    for variant in ("semantic", "semantic_time"):
        groups[variant] = rows(base / f"{variant}.groups.jsonl")
        memberships[variant] = {}
        for g in groups[variant]:
            g["members"] = [ix[uid] for uid in g.pop("unit_ids")]
            for i in g["members"]:
                assert i not in memberships[variant]
                memberships[variant][i] = g["group_id"]
        assert len(memberships[variant]) == len(units)
        neighbors[variant] = [[] for _ in units]
        for n in rows(base / f"{variant}.neighbors.jsonl"):
            neighbors[variant][ix[n["left"]]].append({"i": ix[n["right"]],
                **{k: n[k] for k in ("cosine", "gap_days", "time_multiplier", "score")}})
    for i, u in enumerate(units):
        u.pop("embedding_text")  # Original fragment and exact source offsets remain.
        u["groups"] = {v: memberships[v][i] for v in groups}
    summaries = {}
    for path in sorted(root.glob("summaries*/results.json")):
        for result in read(path):
            group = result["group_id"]
            if group not in summaries or result["status"] == "succeeded":
                summaries[group] = {**result, "run": path.parent.name}
    assessment = read(root / "review/summary-assessment.json")
    for gid, summary in summaries.items():
        summary["assessment"] = next((v for k, v in assessment["assessments"].items() if gid.startswith(k)), None)
    # Export a whitelist, never authorization headers, cookies, .env or hidden reasoning.
    calls = []
    for call in read(root / "api-all-audit.json"):
        response = call.get("response") or {}
        body = response.get("body") or {}
        choice = (body.get("choices") or [{}])[0]
        request = call["request"]
        payload = next((json.loads(m["content"]) for m in request["messages"] if m["role"] == "user"), {})
        calls.append({"call_id": call["call_id"], "group_id": payload.get("group_id"),
                      "created_at": call["created_at"], "elapsed_seconds": call["elapsed_seconds"],
                      "request": request, "error": call.get("error"),
                      "status_code": response.get("status_code"), "usage": body.get("usage"),
                      "finish_reason": choice.get("finish_reason"),
                      "content": choice.get("message", {}).get("content")})
    changes = read(root / "visualization/pair-changes.json")
    source_counts = dict(Counter(d["source"] for d in documents.values()))
    data = {"schema": "semantic-interactive-report.v1", "units": units, "documents": documents,
            "groups": groups, "neighbors": neighbors, "summaries": summaries, "calls": calls,
            "coordinates": projection["coordinates"],
            "projection": {k: v for k, v in projection.items() if k not in ("coordinates", "unit_ids")},
            "aggregate": read(root / "aggregate.json"), "source_counts": source_counts,
            "reviews": read(root / "review/annotations.json"), "issues": issues,
            "pair_changes": {"semantic_pairs": changes["semantic_pairs"],
                             "semantic_time_pairs": changes["semantic_time_pairs"],
                             "lost": len(changes["lost_unit_pairs"]), "gained": len(changes["gained_unit_pairs"])},
            "thresholds": read(root / "chosen-threshold.json"), "temporal": read(root / "chosen-temporal.json"),
            "resource_series": {name: read(root / f"metrics/{name}.json") for name in
                                ("sample-complete", "full-complete", "full-chosen", "api-compact-parallel")},
            "report_text": (root / "REPORT.md").read_text(),
            "provenance": {"experiment_commit": read(root / "aggregate.json")["git_commit"],
                           "model": "BAAI/bge-m3", "revision": "5617a9f61b028005a4858fdac845db406aefb181",
                           "seed": SEED, "python": platform.python_version(), "numpy": np.__version__,
                           "sklearn": sklearn.__version__}}
    artifact_names = ["REPORT.md", "aggregate.json", "ARTIFACT_MANIFEST.json", "environment.freeze.txt",
                      "chosen-threshold.json", "chosen-temporal.json", "review/annotations.json",
                      "review/summary-assessment.json", "review/examples.json", "api-all-audit.json",
                      "visualization/index.json", "visualization/pair-changes.json"]
    data["artifacts"] = [{"name": name, "href": os.path.relpath(root / name, out)} for name in artifact_names]
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    html = (ASSETS / "index.html").read_text().replace("/*__STYLE__*/", (ASSETS / "style.css").read_text())
    html = html.replace("/*__DATA__*/", serialized).replace("/*__APP__*/", (ASSETS / "app.js").read_text())
    (out / "index.html").write_text(html)
    inputs = [*base.glob("*.jsonl"), root / "aggregate.json", root / "api-all-audit.json",
              root / "review/annotations.json", root / "review/summary-assessment.json",
              root / "chosen-threshold.json", root / "chosen-temporal.json", root / "REPORT.md",
              root / "visualization/pair-changes.json", *root.glob("summaries*/results.json"),
              *[root / f"metrics/{name}.json" for name in data["resource_series"]]]
    dump(out / "manifest.json", {"schema": data["schema"], "unit_count": len(ids),
        "document_count": len(documents), "api_call_count": len(calls), "issues": len(issues),
        "inputs": {str(p.relative_to(root)): digest(p) for p in sorted(inputs)},
        "builder_sha256": digest(Path(__file__)),
        "issue_catalog_sha256": digest(out / "issues.json"),
        "assets": {p.name: digest(p) for p in ASSETS.iterdir() if p.is_file()},
        "output_sha256": digest(out / "index.html"), "projection_sha256": digest(out / "projection.json"),
        "privacy": "Local news/request data included. No credentials, response headers, databases or hidden reasoning exported."})
    print(json.dumps({"output": str(out / "index.html"), "bytes": (out / "index.html").stat().st_size,
                      "projection": data["projection"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, default=Path("data/processed/event_pipeline/gpu-experiment"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/event_pipeline/interactive-report-20261023"))
    parser.add_argument("--issues", type=Path, help="Source-bearing curated issue catalog; defaults to OUTPUT/issues.json")
    args = parser.parse_args()
    build(args.experiment, args.output, args.issues)
