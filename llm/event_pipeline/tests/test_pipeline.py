import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from llm.event_pipeline.demo import synthetic_corpus, DemoProvider, response
from llm.event_pipeline.pipeline import (
    plan_run,
    extract_mentions,
    cluster_mentions,
    status,
    read,
    locked,
    connect,
    load_mentions,
)
from llm.event_pipeline.output import export_run, evaluate_run
from llm.event_pipeline.runtime import OfflineProvider, ProviderResponse


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.docs = synthetic_corpus(self.root / "corpus")
        self.run = self.root / "run"
        plan_run(self.root / "corpus", self.run)

    def test_end_to_end_resume_export(self):
        provider = DemoProvider()
        extract_mentions(self.run, provider, 1)
        self.assertEqual(status(self.run)["extracted_documents"], 1)
        extract_mentions(self.run, provider, 1)
        self.assertEqual(len(provider.requests), 2)
        extract_mentions(self.run, provider, 1)
        self.assertEqual(len(provider.requests), 2)
        cluster_mentions(self.run, provider, 1)
        self.assertEqual(len(provider.requests), 3)
        cluster_mentions(self.run, provider, 1)
        self.assertEqual(len(provider.requests), 3)
        result = export_run(self.run, self.root / "out")
        self.assertTrue(result["complete"])
        self.assertEqual(
            len((self.root / "out" / "clusters.jsonl").read_text().splitlines()), 2
        )
        with self.assertRaises(ValueError):
            export_run(self.run, self.root / "out")
        with self.assertRaises(ValueError):
            extract_mentions(self.run, provider, 1)

    def test_partial_export_keeps_unprocessed_mentions(self):
        extract_mentions(self.run, DemoProvider(), 10)
        result = export_run(self.run, self.root / "out")
        self.assertFalse(result["complete"])
        clusters = [
            json.loads(s)
            for s in (self.root / "out" / "clusters.jsonl").read_text().splitlines()
        ]
        self.assertEqual(sum(len(c["mention_ids"]) for c in clusters), 2)

    def test_bad_json_no_retry(self):
        p = OfflineProvider([response("bad")])
        # Valid JSON scalar is saved then rejected by extraction structure.
        extract_mentions(self.run, p, 1)
        self.assertEqual(status(self.run)["mentions"], 0)
        self.assertEqual(len(p.requests), 1)

    def test_length_and_429(self):
        p = OfflineProvider(
            [
                ProviderResponse(
                    {
                        "choices": [
                            {"finish_reason": "length", "message": {"content": "{}"}}
                        ]
                    }
                )
            ]
        )
        extract_mentions(self.run, p, 1)
        self.assertEqual(status(self.run)["mentions"], 0)
        p = OfflineProvider([ProviderResponse({}, 429, {"retry-after": "10"})])
        extract_mentions(self.run, p, 1)
        self.assertEqual(status(self.run)["extracted_documents"], 1)
        self.assertEqual(len(p.requests), 1)

    def test_input_hash_and_plan_idempotency(self):
        plan = read(self.run / "plan.json")
        self.assertEqual(plan_run(self.root / "corpus", self.run), plan)
        with (self.root / "corpus" / "documents.jsonl").open("a") as f:
            f.write(" ")
        with self.assertRaises(ValueError):
            plan_run(self.root / "corpus", self.root / "other")

    def test_lock(self):
        with locked(self.run):
            with self.assertRaises(ValueError):
                extract_mentions(self.run, DemoProvider(), 1)

    def test_preserved_call_after_apply_crash(self):
        p = DemoProvider()
        extract_mentions(self.run, p, 10)
        real = connect
        count = 0

        def faulty(root):
            nonlocal count
            count += 1
            # First checkpoint succeeds; second call response saved before apply failure.
            if count == 4:
                raise RuntimeError("simulated crash")
            return real(root)

        with patch("llm.event_pipeline.pipeline.connect", side_effect=faulty):
            try:
                cluster_mentions(self.run, p, 10)
            except RuntimeError:
                pass
        before = len(p.requests)
        cluster_mentions(self.run, p, 10)
        self.assertLessEqual(len(p.requests) - before, 1)

    def test_evaluation_ignores_unlabelled_pairs(self):
        extract_mentions(self.run, DemoProvider(), 10)
        ms = load_mentions(self.run)
        gold = {
            "mentions": [
                {
                    "id": "g",
                    "document_id": ms[0].document_id,
                    "block_ids": ms[0].block_ids,
                    "event_id": "e",
                }
            ]
        }
        result = evaluate_run(self.run, gold)
        self.assertEqual(result["mention_recall"], 1)
        self.assertEqual(result["pairs"], [])


if __name__ == "__main__":
    unittest.main()
