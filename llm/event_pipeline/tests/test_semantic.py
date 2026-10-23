import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm.event_pipeline.semantic.core import (
    group_vectors,
    normalize,
    pair_score,
    publication,
)
from llm.event_pipeline.semantic.experiment import (
    plan,
    embed,
    compare,
    load_plan,
    make_units,
)
from llm.event_pipeline.demo import synthetic_corpus

UNKNOWN = {"precision": "unknown", "value": None}


class FakeEncoder:
    profile = {"model": "offline-fixture", "revision": "1"}

    def __init__(self):
        self.calls = []

    def encode(self, texts):
        self.calls.append(texts)
        return [[1, 0] for _ in texts], [len(t) for t in texts]


class SemanticTests(unittest.TestCase):
    def test_time_and_missing_dates(self):
        a = {"precision": "timestamp", "value": "2026-01-01T00:00:00+03:00"}
        b = {"precision": "timestamp", "value": "2026-01-08T00:00:00+03:00"}
        self.assertAlmostEqual(pair_score([1, 0], [1, 0], a, b)["score"], 0.8)
        self.assertEqual(pair_score([1, 0], [1, 0], a, UNKNOWN)["score"], 1)
        c = {"precision": "timestamp", "value": "2025-12-31T21:00:00+00:00"}
        self.assertEqual(pair_score([1, 0], [1, 0], a, c)["gap_days"], 0)
        d = {"precision": "day", "value": "2026-01-01"}
        self.assertEqual(pair_score([1, 0], [1, 0], a, d)["gap_days"], 0)

    def test_complete_link_blocks_transitive_chain(self):
        vectors = [[math.cos(t), math.sin(t)] for t in (0, 0.5, 1)]
        result = group_vectors(vectors, [UNKNOWN] * 3, threshold=0.8)
        self.assertEqual(sorted(map(len, result["groups"])), [1, 2])
        self.assertTrue(all(s is None or s >= 0.8 for s in result["minimum_scores"]))

    def test_temporal_variant_splits_same_vectors(self):
        dates = [{"precision": "day", "value": v} for v in ("2026-01-01", "2026-03-01")]
        self.assertEqual(len(group_vectors([[1, 0]] * 2, dates)["groups"]), 1)
        self.assertEqual(
            len(group_vectors([[1, 0]] * 2, dates, alpha=0.4)["groups"]), 2
        )

    def test_limits_and_invalid_vectors(self):
        for vector in ([0, 0], [float("nan")], [True], [float("inf")]):
            with self.assertRaises(ValueError):
                normalize(vector)
        with self.assertRaises(ValueError):
            group_vectors([[1], [1, 0]], [UNKNOWN] * 2)
        with self.assertRaises(ValueError):
            group_vectors([[1]], [UNKNOWN], max_units=0)
        with self.assertRaises(ValueError):
            group_vectors([[1]], [UNKNOWN], half_life_days=0)
        self.assertEqual(group_vectors([], [])["groups"], [])

    def test_source_slices_technical_title_and_day_precision(self):
        doc = {
            "document_id": "d",
            "article_sha256": "a",
            "article": {
                "title": "Telegram #1",
                "text": "😀 Один\u00a0текст.\n\nДругой абзац.",
                "published_at": None,
            },
            "provenance": [
                {
                    "title_origin": "generated_identifier",
                    "publication": {"date": "2026-10-23"},
                }
            ],
        }
        self.assertEqual(publication(doc)["precision"], "day")
        for unit in make_units(doc, 10):
            self.assertEqual(
                unit["text"], doc["article"][unit["field"]][unit["start"] : unit["end"]]
            )
            self.assertNotIn("Telegram", unit["embedding_text"])
        self.assertEqual(len(make_units(doc, 100)), 1)

    def test_resume_cache_export_and_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            synthetic_corpus(root / "corpus")
            run = root / "run"
            plan(root / "corpus", run)
            provider = FakeEncoder()
            self.assertFalse(
                embed(run, provider, batch_size=1, max_batches=1)["complete"]
            )
            with self.assertRaisesRegex(ValueError, "incomplete"):
                compare(run, root / "incomplete", backend="reference")
            self.assertTrue(
                embed(run, provider, batch_size=1, max_batches=1)["complete"]
            )
            embed(run, provider)
            self.assertEqual(len(provider.calls), 2)
            report = compare(run, root / "out", backend="reference")
            self.assertEqual(report["usage"]["encoded_texts"], 2)
            self.assertEqual(report["variants"]["semantic"]["groups"], 1)
            self.assertEqual(
                len((root / "out" / "vectors.jsonl").read_text().splitlines()), 2
            )
            with self.assertRaisesRegex(ValueError, "output_exists"):
                compare(run, root / "out", backend="reference")
            provider.profile = {"model": "changed"}
            with self.assertRaisesRegex(ValueError, "encoder_changed"):
                embed(run, provider)
            value = json.loads((run / "plan.json").read_text())
            value["units"][0]["text"] = "changed"
            (run / "plan.json").write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "plan_hash"):
                load_plan(run)

    def test_invalid_batch_is_not_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            synthetic_corpus(root / "corpus")
            plan(root / "corpus", root / "run")
            provider = FakeEncoder()
            with patch.object(provider, "encode", return_value=([[1]], [1])):
                with self.assertRaisesRegex(ValueError, "count_mismatch"):
                    embed(root / "run", provider)
            self.assertEqual(embed(root / "run", provider)["new_vectors"], 2)

    def test_failure_preserves_previous_batches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            synthetic_corpus(root / "corpus")
            plan(root / "corpus", root / "run")
            provider = FakeEncoder()
            original = provider.encode
            calls = 0

            def fail_second(texts):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated_crash")
                return original(texts)

            with patch.object(provider, "encode", side_effect=fail_second):
                with self.assertRaises(RuntimeError):
                    embed(root / "run", provider, batch_size=1)
            result = embed(root / "run", provider, batch_size=1)
            self.assertEqual(result["new_vectors"], 1)
            self.assertTrue(result["complete"])

    def test_encoder_rejects_truncation_before_encode(self):
        from llm.event_pipeline.semantic.encoder import SentenceEncoder
        from unittest.mock import Mock

        encoder = object.__new__(SentenceEncoder)
        encoder.model = Mock()
        encoder.model.max_seq_length = 2
        encoder.model.tokenizer.return_value = {"input_ids": [1, 2, 3]}
        with self.assertRaisesRegex(ValueError, "too_long"):
            encoder.encode(["long source"])
        encoder.model.encode.assert_not_called()
        encoder.model.tokenizer.assert_called_once_with(
            "long source", truncation=False, add_special_tokens=True
        )

    def test_scipy_matches_reference_when_available(self):
        import importlib.util

        if importlib.util.find_spec("scipy") is None:
            self.skipTest("optional scipy environment")
        vectors = [[math.cos(t), math.sin(t)] for t in (0, 0.3, 0.7, 1.4)]
        reference = group_vectors(vectors, [UNKNOWN] * 4, threshold=0.8)
        accelerated = group_vectors(
            vectors, [UNKNOWN] * 4, threshold=0.8, backend="scipy"
        )
        self.assertEqual(reference["groups"], accelerated["groups"])

    def test_vectorized_time_matches_reference_for_intervals_and_unknown(self):
        import importlib.util
        if importlib.util.find_spec('scipy') is None:
            self.skipTest('optional scipy environment')
        vectors = [[math.cos(t), math.sin(t)] for t in (0, 0.13, 0.4, 0.9, 1.8)]
        dates = [UNKNOWN,
                 {'precision':'day','value':'2026-01-01'},
                 {'precision':'timestamp','value':'2025-12-31T21:00:00+00:00'},
                 {'precision':'timestamp','value':'2026-01-08T15:00:00+03:00'},
                 {'precision':'day','value':'2026-02-01'}]
        for alpha in (0.2, 0.4, 0.6):
            for half_life in (1, 7, 30):
                kwargs=dict(threshold=0.65,alpha=alpha,half_life_days=half_life,neighbors=4)
                expected=group_vectors(vectors,dates,backend='reference',**kwargs)
                actual=group_vectors(vectors,dates,backend='scipy',**kwargs)
                self.assertEqual(expected['groups'],actual['groups'])
                for left,right in zip(expected['minimum_scores'],actual['minimum_scores']):
                    if left is not None: self.assertAlmostEqual(left,right)
                self.assertEqual(expected['neighbors'],actual['neighbors'])
