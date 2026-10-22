import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from llm.event_pipeline.demo import synthetic_corpus, DemoProvider, response
from llm.event_pipeline.pipeline import (
    plan_run,
    extract_mentions,
    cluster_mentions,
    load_mentions,
    connect,
    read,
)
from llm.event_pipeline.blocks import source_blocks, fit_packets
from llm.event_pipeline.runtime import (
    CreditLedger,
    execute,
    request,
    EventStore,
    ProviderProfile,
    ExecutionLimits,
    OfflineProvider,
    ProviderResponse,
)
from llm.events.extraction.credits import RequestCounter
from llm.events.storage.store import StorageError


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.docs = synthetic_corpus(self.root / "corpus")
        self.run = self.root / "run"
        plan_run(self.root / "corpus", self.run)

    def test_shared_counter_atomic_and_never_created(self):
        store = EventStore(self.run / "jobs.sqlite3")
        with self.assertRaises(sqlite3.OperationalError):
            CreditLedger(store, self.root / "absent.sqlite")
        self.assertFalse((self.root / "absent.sqlite").exists())
        counter = RequestCounter(self.root / "quota.sqlite", 1)
        ledgers = [CreditLedger(store, counter.path), CreditLedger(store, counter.path)]

        def reserve(ledger):
            try:
                return ledger.counter.reserve({"test": True})
            except StorageError:
                return None

        with ThreadPoolExecutor(2) as pool:
            values = list(pool.map(reserve, ledgers))
        self.assertEqual(sum(v is not None for v in values), 1)
        self.assertEqual(len(counter.summary()), 1)
        self.assertIsNone(counter.summary()[0]["response"])

    def test_split_before_request_and_indivisible_deferred(self):
        doc = self.docs[0]
        doc["article"]["text"] = "A" * 90 + "\n\n" + "B" * 90 + "\n\n" + "C" * 90
        bs = source_blocks(doc, 100)
        ps = list(fit_packets(bs, 8000, 500, lambda p: p))
        self.assertGreater(len(ps), 1)
        small = list(fit_packets(bs, 8000, 10, lambda p: p))
        self.assertTrue(
            all(p.get("deferred_reason") == "input_too_large" for p in small)
        )
        self.assertEqual(
            {b.block_id for b in bs if b.field == "text"},
            {i for p in small for i in p["primary"]},
        )

    def test_partial_document_checkpoint(self):
        # Several packets with one API slot: accepted mentions are retained now.
        import hashlib

        corpus = self.root / "corpus"
        docs = self.docs
        docs[0]["article"]["text"] = "Первая новость.\n\nВторая новость."
        from llm.events.contracts.validation import article_sha256
        from llm.events.contracts.models import ArticleInput

        docs[0]["article_sha256"] = article_sha256(
            ArticleInput.model_validate(docs[0]["article"])
        )
        raw = "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in docs).encode()
        (corpus / "documents.jsonl").write_bytes(raw)
        m = read(corpus / "manifest.json")
        m["outputs"]["documents.jsonl"] = hashlib.sha256(raw).hexdigest()
        (corpus / "manifest.json").write_text(json.dumps(m))
        run = self.root / "multi"
        plan_run(corpus, run, configuration={"packet_limit": 16})
        p = DemoProvider()
        extract_mentions(run, p, 1)
        self.assertEqual(len(load_mentions(run)), 1)
        extract_mentions(run, p, 1)
        self.assertEqual(len(p.requests), 2)
        self.assertEqual(len(load_mentions(run)), 2)

    def test_malformed_response_does_not_leave_running_job(self):
        p = OfflineProvider(
            [
                ProviderResponse(
                    {
                        "choices": [
                            {"finish_reason": "stop", "message": {"content": "{broken"}}
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                    }
                )
            ]
        )
        extract_mentions(self.run, p, 1)
        with closing(sqlite3.connect(self.run / "jobs.sqlite3")) as db:
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE state='running'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE kind='llm_response'"
                ).fetchone()[0],
                1,
            )

    def test_conflicting_values_can_merge(self):
        class Provider(DemoProvider):
            def complete(self, req):
                r = super().complete(req)
                data = json.loads(r.body["choices"][0]["message"]["content"])
                for d in data.get("decisions", []):
                    d.update(identity="same", claim_compatibility="conflicting")
                return response(data)

        p = Provider()
        extract_mentions(self.run, p, 10)
        cluster_mentions(self.run, p, 10)
        with connect(self.run) as db:
            steps = [json.loads(r[0]) for r in db.execute("SELECT payload FROM steps")]
        self.assertEqual(
            steps[0]["cluster"]["cluster_id"], steps[1]["cluster"]["cluster_id"]
        )
        self.assertEqual(
            steps[1]["cluster"]["decisions"][0]["claim_compatibility"], "conflicting"
        )

    def test_crash_after_model_result_reuses_exact_artifact(self):
        p = DemoProvider()
        extract_mentions(self.run, p, 10)
        from llm.event_pipeline import pipeline

        with patch.object(
            pipeline, "parse_decisions", side_effect=RuntimeError("crash")
        ):
            with self.assertRaises(RuntimeError):
                cluster_mentions(self.run, p, 10)
        count = len(p.requests)
        cluster_mentions(self.run, p, 10)
        self.assertEqual(len(p.requests), count)

    def test_incomplete_verifier_keeps_both_mentions(self):
        class Provider(DemoProvider):
            def complete(self, req):
                if "clusters" in json.loads(req["messages"][1]["content"]):
                    return response({"decisions": []})
                return super().complete(req)

        p = Provider()
        extract_mentions(self.run, p, 10)
        cluster_mentions(self.run, p, 10)
        with connect(self.run) as db:
            steps = [json.loads(r[0]) for r in db.execute("SELECT payload FROM steps")]
        self.assertEqual(len(steps), 2)
        self.assertNotEqual(
            steps[0]["cluster"]["cluster_id"], steps[1]["cluster"]["cluster_id"]
        )
        self.assertEqual(steps[1]["reason"], "invalid_verification")

    def test_late_live_response_is_audited_but_not_accepted(self):
        store = EventStore(self.run / "jobs.sqlite3")
        rec = store.get_current_article(self.docs[0]["article"]["article_id"])
        profile = ProviderProfile(execution_mode="live")
        counter = RequestCounter(self.root / "late-quota.sqlite", 1)

        class FakeLive:
            is_live = True

            def __init__(self):
                self.profile = profile

            def check_credentials(self):
                pass

            def complete(self, req):
                changed = dict(
                    rec.article.model_dump(mode="json"), text="Исправленная версия"
                )
                store.import_batch(json.dumps([changed]).encode())
                return response({"mentions": []})

        result = execute(
            store,
            rec.source_id,
            "extract",
            request(profile, "test", {}),
            profile,
            FakeLive(),
            ExecutionLimits(live_enabled=True),
            counter.path,
        )
        self.assertEqual(result["status"], "superseded")
        self.assertEqual(len(counter.summary()), 1)
        self.assertIsNotNone(counter.summary()[0]["response"])
        self.assertIsNone(result["artifact_id"])

    def test_timeout_reservation_not_released(self):
        from llm.events.extraction.provider import ProviderFailure

        store = EventStore(self.run / "jobs.sqlite3")
        rec = store.get_current_article(self.docs[0]["article"]["article_id"])
        profile = ProviderProfile(execution_mode="live")
        counter = RequestCounter(self.root / "timeout-quota.sqlite", 1)

        class FakeLive:
            is_live = True

            def __init__(self):
                self.profile = profile

            def check_credentials(self):
                pass

            def complete(self, req):
                raise ProviderFailure("provider_timeout", "timeout")

        result = execute(
            store,
            rec.source_id,
            "extract",
            request(profile, "test", {}),
            profile,
            FakeLive(),
            ExecutionLimits(live_enabled=True),
            counter.path,
        )
        self.assertTrue(result["stop"])
        with self.assertRaises(StorageError):
            counter.reserve({})
        self.assertEqual(counter.summary()[0]["error"]["code"], "provider_timeout")
