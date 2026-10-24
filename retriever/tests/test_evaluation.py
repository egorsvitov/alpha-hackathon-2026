import json
from unittest.mock import patch

import pytest

from retriever.evaluation import evaluate_batch, make_review_pool, read_batch
from retriever.example import run_example
from retriever.models import RetrievalRun
from retriever.output import write_run


def fixture(tmp_path):
    run_example(tmp_path / "demo")
    path = tmp_path / "demo" / "batch.json"
    return path, read_batch(path)


def test_unjudged_are_not_negatives_and_pool_deduplicates(tmp_path):
    path, batch = fixture(tmp_path)
    pool = make_review_pool([path, path])
    assert len(pool["cases"]) == 2
    result = evaluate_batch(batch, pool)
    assert result["judged_precision"] is None
    assert result["known_positive_pool_recall"] is None
    assert result["unjudged"] == 2
    assert len(pool["cases"][0]["appearances"]) == 2


def test_partial_labels_and_known_positive_recall(tmp_path):
    path, batch = fixture(tmp_path)
    pool = make_review_pool([path])
    from retriever.evaluation import case_key

    first = case_key(batch, batch.candidates[0])
    for row in pool["cases"]:
        row["relevance"] = "relevant"
        row["story_id"] = "same-manually-confirmed-event"
        row["merge_quality"] = "false_merge" if row["case_id"] == first else "correct"
        row["detail_loss"] = "yes" if row["case_id"] == first else "no"
    report = evaluate_batch(batch, pool, top_k=1)
    assert report["known_positive_pool_recall"] == 0.5
    assert report["judged_precision"] == 1
    assert report["false_merges"] == 1
    assert report["detail_loss"] == 1
    assert evaluate_batch(batch, pool)["repeated_labeled_stories"] == 1


def test_changed_batch_and_invalid_labels_rejected(tmp_path):
    path, batch = fixture(tmp_path)
    pool = make_review_pool([path])
    with pytest.raises(ValueError, match="batch_not_in_review_pool"):
        evaluate_batch(batch.model_copy(update={"company_id": "other"}), pool)
    pool["cases"][0]["relevance"] = "maybe"
    with pytest.raises(ValueError, match="invalid_relevance_label"):
        evaluate_batch(batch, pool)


def test_batch_missing_sources_rejected(tmp_path):
    _path, batch = fixture(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text(batch.model_copy(update={"sources": []}).model_dump_json())
    with pytest.raises(ValueError, match="missing_candidate_sources"):
        read_batch(bad)


def test_atomic_output_and_existing_export_preserved(tmp_path):
    _, batch = fixture(tmp_path)
    output = tmp_path / "ready"
    run = RetrievalRun(batch=batch, trace={"calls_new": 0})
    write_run(run, output)
    manifest = json.loads((output / "manifest.json").read_text())
    from retriever.models import text_hash

    for name, hash_ in manifest["files"].items():
        assert text_hash((output / name).read_text()) == hash_
    before = (output / "batch.json").read_bytes()
    with pytest.raises(ValueError, match="output_already_exists"):
        write_run(run, output)
    assert (output / "batch.json").read_bytes() == before


def test_failure_before_publish_leaves_no_ready_output(tmp_path):
    _, batch = fixture(tmp_path)
    output = tmp_path / "failed"
    with (patch("retriever.output.atomic_json", side_effect=OSError("disk full")),
          pytest.raises(OSError)):
        write_run(RetrievalRun(batch=batch, trace={}), output)
    assert not output.exists()
    assert not list(tmp_path.glob(".failed-*"))
