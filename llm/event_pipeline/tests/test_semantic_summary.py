import json
from pathlib import Path
import tempfile
import unittest

from llm.event_pipeline.semantic.summary import (
    ExperimentCounter,
    parse_summary,
    SummaryRunner,
)
from llm.event_pipeline.runtime import (
    EventStore,
    ProviderProfile,
    ExecutionLimits,
    OfflineProvider,
    ProviderResponse,
)
from llm.events.storage.store import StorageError, canonical_json
from llm.events.storage.models import ProcessingSpec
from llm.event_pipeline.demo import synthetic_corpus


class SummaryTests(unittest.TestCase):
    def valid(self):
        return {
            "theme": "Тема",
            "items": [
                {
                    "text": "Сообщение",
                    "kind": "reported_fact",
                    "references": [{"unit_id": "u", "document_id": "d"}],
                }
            ],
            "differences": [],
            "contradictions": [],
            "limitations": [],
        }

    def test_references_are_not_grounding(self):
        value = parse_summary(
            json.dumps(self.valid()),
            [
                {"unit_id": "u", "document_id": "d"},
                {"unit_id": "v", "document_id": "e"},
            ],
        )
        self.assertEqual(value["semantic_support"], "unreviewed")
        self.assertEqual(value["unreferenced_units"], ["v"])
        bad = self.valid()
        bad["items"][0]["references"][0]["document_id"] = "other"
        with self.assertRaisesRegex(StorageError, "Unknown"):
            parse_summary(json.dumps(bad), [{"unit_id": "u", "document_id": "d"}])
        for raw in ("{", "{}", "null"):
            with self.assertRaises(StorageError):
                parse_summary(raw, [])

    def test_quota_is_immutable_and_reservation_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "quota.sqlite3"
            c = ExperimentCounter(p, 2)
            cid = c.reserve({"messages": []})
            c.finish(cid, None, {"code": "timeout"}, 0.1)
            c = ExperimentCounter(p, 2)
            c.reserve({"messages": []})
            with self.assertRaises(StorageError):
                c.reserve({})
            with self.assertRaises(StorageError):
                ExperimentCounter(p, 200)
            self.assertEqual(len(c.summary()), 2)
            with self.assertRaises(StorageError):
                ExperimentCounter(Path(tmp) / "over.sqlite3", 201)

    def test_invalid_json_is_audited_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            synthetic_corpus(root / "corpus")
            docs = [
                json.loads(x)
                for x in (root / "corpus/documents.jsonl").read_text().splitlines()
            ]
            store = EventStore(root / "jobs.sqlite3")
            store.import_batch(canonical_json([d["article"] for d in docs]).encode())
            from llm.events.storage.store import source_id_for

            sid = source_id_for(docs[0]["article"]["article_id"], 1)
            profile = ProviderProfile(
                execution_mode="offline",
                limits_confirmed=True,
                max_input_tokens=30000,
                max_output_tokens=4096,
                context_window_tokens=34096,
            )
            spec = ProcessingSpec(
                processor_name="summary.test",
                processor_version="1",
                configuration={
                    "profile": profile.model_dump(mode="json"),
                    "request": {
                        "model": profile.model,
                        "messages": [],
                        "max_tokens": 100,
                    },
                    "units": [{"unit_id": "u", "document_id": "d"}],
                },
            )
            store.enqueue(sid, spec)
            provider = OfflineProvider(
                [
                    ProviderResponse(
                        {
                            "choices": [
                                {
                                    "finish_reason": "stop",
                                    "message": {"content": "{broken"},
                                }
                            ],
                            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                        }
                    )
                ]
            )
            runner = SummaryRunner(store, provider)
            limits = ExecutionLimits(
                tariffs_confirmed=True,
                input_rub_per_million="0",
                output_rub_per_million="0",
                daily_quota_rub="1",
            )
            result = runner.run_next(spec, limits, source_id=sid)
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "summary_format_invalid")
            self.assertEqual(
                runner.run_next(spec, limits, source_id=sid).status, "idle"
            )
            self.assertEqual(len(provider.requests), 1)

    def test_live_adapter_uses_new_durable_cap_without_reset_or_second_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            synthetic_corpus(root / "corpus")
            docs = [
                json.loads(x)
                for x in (root / "corpus/documents.jsonl").read_text().splitlines()
            ]
            store = EventStore(root / "jobs.sqlite3")
            store.import_batch(canonical_json([d["article"] for d in docs]).encode())
            from llm.events.storage.store import source_id_for

            profile = ProviderProfile()

            class SimulatedLive(OfflineProvider):
                is_live = True

                def check_credentials(self):
                    pass

            body = {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(self.valid())},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
            provider = SimulatedLive([ProviderResponse(body)])
            provider.profile = profile
            counter = ExperimentCounter(root / "quota.sqlite3", 1)
            runner = SummaryRunner(store, provider, journal=counter.path)
            for index, d in enumerate(docs):
                sid = source_id_for(d["article"]["article_id"], 1)
                spec = ProcessingSpec(
                    processor_name="summary.credit.test",
                    processor_version="1",
                    configuration={
                        "profile": profile.model_dump(mode="json"),
                        "request": {"model": profile.model, "messages": []},
                        "units": [{"unit_id": "u", "document_id": "d"}],
                        "group": index,
                    },
                )
                store.enqueue(sid, spec)
                result = runner.run_next(
                    spec, ExecutionLimits(live_enabled=True), source_id=sid
                )
                if index == 0:
                    self.assertEqual(result.status, "succeeded")
                else:
                    self.assertEqual(result.reason, "budget_quota_exhausted")
                    self.assertTrue(result.stop)
            self.assertEqual(len(provider.requests), 1)
            audit = ExperimentCounter(counter.path, 1).summary()
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["response"]["body"]["usage"]["total_tokens"], 30)

    def test_parallel_workers_cannot_overshoot_shared_quota(self):
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shared.sqlite3"
            ExperimentCounter(path, 3)

            def reserve(index):
                try:
                    return ExperimentCounter(path, 3).reserve({"worker": index})
                except StorageError as error:
                    self.assertEqual(error.code, "pilot_limit_exhausted")
                    return None

            with ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(reserve, range(12)))
            self.assertEqual(sum(x is not None for x in outcomes), 3)
            self.assertEqual(len(ExperimentCounter(path, 3).summary()), 3)
