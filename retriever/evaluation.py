"""Judgment pooling and observed-label metrics; unjudged pairs are never negatives."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from .models import RetrievalCandidateBatch, canonical, digest


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_batch(path):
    batch = RetrievalCandidateBatch.model_validate_json(
        Path(path).read_text(encoding="utf-8")
    )
    ids = [c.item.item_id for c in batch.candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_candidate_id")
    sources = {a.revision_id: a for a in batch.sources}
    if len(sources) != len(batch.sources):
        raise ValueError("duplicate_batch_source")
    for candidate in batch.candidates:
        if not set(candidate.item.source_revision_ids) <= sources.keys():
            raise ValueError("missing_candidate_sources")
    return batch


def case_key(batch, candidate):
    # Different companies/profiles/as_of and edited outputs require separate review.
    return digest(
        [
            batch.company_id,
            batch.profile_version,
            batch.interest_set_version,
            batch.corpus_id,
            batch.as_of,
            candidate.item.model_dump(),
        ]
    )


def make_review_pool(paths):
    cases, identities = {}, []
    for path in paths:
        batch = read_batch(path)
        identities.append(digest(batch))
        sources = {a.revision_id: a.model_dump() for a in batch.sources}
        for rank, candidate in enumerate(batch.candidates, 1):
            key = case_key(batch, candidate)
            if key not in cases:
                cases[key] = {
                    "case_id": key,
                    "company_id": batch.company_id,
                    "profile_version": batch.profile_version,
                    "interest_set_version": batch.interest_set_version,
                    "as_of": batch.as_of,
                    "corpus_id": batch.corpus_id,
                    "item": candidate.item.model_dump(),
                    "sources": [sources[k] for k in candidate.item.source_revision_ids],
                    "appearances": [],
                    "relevance": None,
                    "merge_quality": None,
                    "detail_loss": None,
                    "story_id": None,
                    "note": "",
                }
            cases[key]["appearances"].append(
                {
                    "request_id": batch.request_id,
                    "rank": rank,
                    "best_interest_id": candidate.best_interest_id,
                    "interest_scores": candidate.interest_scores,
                }
            )
    return {
        "schema_version": "retriever-review-pool.v1",
        "batch_hashes": sorted(set(identities)),
        "annotation_status": "unannotated",
        "cases": [cases[k] for k in sorted(cases)],
        "instructions": {
            "relevance": ["relevant", "irrelevant", "uncertain"],
            "merge_quality": ["correct", "false_merge", "false_split", "uncertain"],
            "detail_loss": ["yes", "no", "uncertain"],
            "story_id": "Optional manual event identity. Do not infer from shared source IDs.",
            "company_profile": "Reviewer must also read the actual company interests/profile.",
            "missing_labels": "Leave null; unjudged cases are not negatives.",
        },
    }


def evaluate_batch(batch, pool, *, top_k=None):
    if pool.get("schema_version") != "retriever-review-pool.v1":
        raise ValueError("unsupported_review_schema")
    if digest(batch) not in pool["batch_hashes"]:
        raise ValueError("batch_not_in_review_pool")
    labels = {}
    for row in pool["cases"]:
        if row["case_id"] in labels:
            raise ValueError("duplicate_review_case")
        if row.get("relevance") not in (None, "relevant", "irrelevant", "uncertain"):
            raise ValueError("invalid_relevance_label")
        if row.get("merge_quality") not in (
            None,
            "correct",
            "false_merge",
            "false_split",
            "uncertain",
        ):
            raise ValueError("invalid_merge_label")
        if row.get("detail_loss") not in (None, "yes", "no", "uncertain"):
            raise ValueError("invalid_detail_loss_label")
        labels[row["case_id"]] = row
    if top_k is not None and (isinstance(top_k, bool) or top_k < 1):
        raise ValueError("top_k_must_be_positive")
    selected = batch.candidates[:top_k]
    rows = [labels.get(case_key(batch, c), {}) for c in selected]
    relevant = sum(r.get("relevance") == "relevant" for r in rows)
    negative = sum(r.get("relevance") == "irrelevant" for r in rows)
    # Recall is restricted to known positives in this exact company/profile/time/corpus pool.
    scope = [
        r
        for r in pool["cases"]
        if (
            r["company_id"],
            r["profile_version"],
            r["interest_set_version"],
            r["corpus_id"],
            r["as_of"],
        )
        == (
            batch.company_id,
            batch.profile_version,
            batch.interest_set_version,
            batch.corpus_id,
            batch.as_of,
        )
    ]
    known = {r["case_id"] for r in scope if r.get("relevance") == "relevant"}
    positive_stories = {
        r["story_id"]
        for r in scope
        if r.get("relevance") == "relevant" and r.get("story_id")
    }
    selected_stories = {
        r["story_id"]
        for r in rows
        if r.get("relevance") == "relevant" and r.get("story_id")
    }
    stories = [r["story_id"] for r in rows if r.get("story_id")]
    return {
        "schema_version": "retriever-evaluation.v1",
        "request_id": batch.request_id,
        "batch_hash": digest(batch),
        "annotation_status": pool.get("annotation_status", "unknown"),
        "returned": len(selected),
        "requested": batch.requested_k,
        "status": batch.status,
        "judged_positive": relevant,
        "judged_negative": negative,
        "uncertain": sum(r.get("relevance") == "uncertain" for r in rows),
        "unjudged": sum(r.get("relevance") is None for r in rows),
        "judged_precision": relevant / (relevant + negative)
        if relevant + negative
        else None,
        "known_positive_pool_recall": relevant / len(known) if known else None,
        "known_positive_story_recall": len(selected_stories & positive_stories)
        / len(positive_stories)
        if positive_stories
        else None,
        "false_merges": sum(r.get("merge_quality") == "false_merge" for r in rows),
        "false_splits": sum(r.get("merge_quality") == "false_split" for r in rows),
        "detail_loss": sum(r.get("detail_loss") == "yes" for r in rows),
        "repeated_labeled_stories": len(stories) - len(set(stories)),
        "best_interest_counts": {
            i: sum(c.best_interest_id == i for c in selected)
            for i in sorted({c.best_interest_id for c in selected})
        },
        "packet_bytes": len(canonical(batch).encode()),
        "deferred_clusters": len(batch.deferred),
        "limitations": [
            "Known-positive recall applies only to the reviewed pool, not the whole corpus.",
            "Shared sources do not prove two outputs are duplicates.",
            "Counts of merge errors/detail loss require corresponding manual labels.",
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    pool = commands.add_parser("pool")
    pool.add_argument("--batches", nargs="+", required=True)
    pool.add_argument("--output", required=True)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--batch", required=True)
    evaluate.add_argument("--labels", required=True)
    evaluate.add_argument("--top-k", type=int)
    evaluate.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "pool":
        if Path(args.output).exists():
            parser.error("review output already exists; preserve existing annotations")
        result = make_review_pool(args.batches)
    else:
        result = evaluate_batch(
            read_batch(args.batch),
            json.loads(Path(args.labels).read_text()),
            top_k=args.top_k,
        )
    atomic_json(args.output, result)
    print(
        canonical({"output": args.output, "schema_version": result["schema_version"]})
    )


if __name__ == "__main__":
    main()
