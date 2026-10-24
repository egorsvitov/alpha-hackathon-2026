import json
import tempfile
import unittest
from pathlib import Path

from llm.cluster_merge import *
from llm.cluster_merge.core import messages, parse_answer
from llm.cluster_merge.provider import ProviderError


class Quota:
    def __init__(self):
        self.calls = []

    def reserve(self, key):
        self.calls.append(key)


class Provider:
    def __init__(self, split=False, fail=False):
        self.calls = 0
        self.split = split
        self.fail = fail

    def complete(self, messages, profile):
        self.calls += 1
        if self.fail:
            raise TimeoutError()
        fs = json.loads(messages[1]["content"])["fragments"]
        groups = [[f] for f in fs] if self.split else [fs]
        answer = {
            "events": [
                {
                    "title": "Событие",
                    "paragraphs": [
                        {
                            "text": " ".join(f["text"] for f in group),
                            "fragment_ids": [f["id"] for f in group],
                        }
                    ],
                }
                for group in groups
            ],
            "coverage": [{"fragment_id": f["id"], "kind": "used"} for f in fs],
        }
        return {
            "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps(answer)}}
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 30},
        }


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.req = MergeRequest(
            cluster_id="c",
            revision="1",
            trigger_id="a",
            articles=[
                Article(id="a", text="Цена 10 рублей. 😀\u00a0", source="ТАСС"),
                Article(id="b", text="Цена 11 рублей.", source="РИА"),
            ],
        )
        self.p = Provider()
        self.q = Quota()
        self.s = MergeService(self.p, self.q, Path(self.tmp.name) / "state.db")

    def test_merge_and_cache(self):
        r = self.s.merge_cluster(self.req, max_calls=4)
        self.assertEqual((r.status, r.mode), ("completed", "merged"))
        self.assertEqual(r.events[0].publication_count, 2)
        self.assertTrue(validate_merge_result(r, self.req).valid)
        r2 = self.s.merge_cluster(
            self.req.model_copy(update={"trigger_id": "b", "revision": "2"}),
            max_calls=0,
        )
        self.assertTrue(r2.cache_hit)
        self.assertEqual(self.p.calls, 1)
        self.assertEqual(r2.expected_revision, "2")
        self.assertEqual(r.events[0].id, r2.events[0].id)

    def test_coordinates(self):
        for f in plan_merge(self.req).fragments:
            a = next(a for a in self.req.articles if a.id == f.article_id)
            self.assertEqual(getattr(a, f.field)[f.start : f.end], f.text)

    def test_limit_resume(self):
        r = self.s.merge_cluster(self.req, max_calls=0)
        self.assertEqual(r.status, "partial")
        self.assertEqual(self.p.calls, 0)
        self.assertEqual(
            self.s.resume_merge(r.operation_id, max_calls=2).status, "completed"
        )

    def test_unknown_never_retried(self):
        self.p.fail = True
        r = self.s.merge_cluster(self.req, max_calls=2)
        self.assertEqual(r.status, "failed")
        self.s.resume_merge(r.operation_id, max_calls=2)
        self.assertEqual(self.p.calls, 1)

    def test_split(self):
        self.p.split = True
        r = self.s.merge_cluster(self.req, max_calls=5)
        self.assertEqual(r.mode, "split")
        self.assertEqual(len(r.events), 2)

    def test_bad_coverage(self):
        with self.assertRaises(ValueError):
            parse_answer('{"events":[],"coverage":[]}', plan_merge(self.req).fragments)

    def test_unknown_reference(self):
        fs = plan_merge(self.req).fragments
        raw = {
            "events": [
                {"title": "x", "paragraphs": [{"text": "x", "fragment_ids": ["bad"]}]}
            ],
            "coverage": [{"fragment_id": f.id, "kind": "used"} for f in fs],
        }
        with self.assertRaises(ValueError):
            parse_answer(json.dumps(raw), fs)

    def test_concurrent(self):
        op = plan_merge(self.req).operation_id
        self.s.merge_cluster(self.req, max_calls=0)
        with self.s.db() as db:
            db.execute(
                "UPDATE operations SET owner=?,expires=? WHERE id=?",
                ("other", self.s.clock() + 900, op),
            )
        with self.assertRaisesRegex(MergeError, "in_progress"):
            self.s.merge_cluster(self.req, max_calls=1)

    def test_quota(self):
        q = SQLiteQuota(Path(self.tmp.name) / "quota.db", 1)
        q.reserve("1")
        with self.assertRaisesRegex(ProviderError, "quota_exhausted"):
            q.reserve("2")
        with self.assertRaises(ValueError):
            SQLiteQuota(Path(self.tmp.name) / "quota.db", 2)

    def test_profile_key(self):
        changed = self.req.model_copy(update={"profile": Profile(reasoning=True)})
        self.assertNotEqual(
            plan_merge(changed).operation_id, plan_merge(self.req).operation_id
        )

    def test_embedding_failure_keeps_index(self):
        from llm.cluster_merge.example import ExampleIndex

        r = self.s.merge_cluster(self.req, max_calls=4)
        index = ExampleIndex(self.req)

        def fail(text):
            raise RuntimeError("encoder failed")

        with self.assertRaises(RuntimeError):
            index.apply(r, self.req, fail)
        self.assertEqual(index.events, {})
        index.revision = "new"
        with self.assertRaisesRegex(ValueError, "cluster_changed"):
            index.apply(r, self.req, lambda t: [1])
        self.assertEqual(index.events, {})

    def test_pure_merge_selection(self):
        from llm.cluster_merge.example import ExampleIndex

        r = self.s.merge_cluster(self.req, max_calls=4)
        index = ExampleIndex(self.req)
        self.assertEqual(index.apply(r, self.req, lambda t: [1]), r.events[0].id)
        self.assertEqual(len(index.articles), 2)

    def test_saved_response_reapplied(self):
        r = self.s.merge_cluster(self.req, max_calls=3)
        # Simulate crash before applying the saved response to operation state.
        with self.s.db() as db:
            db.execute(
                "UPDATE operations SET state=? WHERE id=?",
                (
                    json.dumps(
                        {
                            "batch": 0,
                            "events": [],
                            "coverage": {},
                            "pairs": {},
                            "blocked": [],
                        }
                    ),
                    r.operation_id,
                ),
            )
        r2 = self.s.resume_merge(r.operation_id, max_calls=0)
        self.assertEqual(r2.status, "completed")
        self.assertEqual(self.p.calls, 1)

    def test_length_never_success(self):
        self.p.complete = lambda *a: {
            "choices": [{"finish_reason": "length", "message": {"content": "{}"}}]
        }
        r = self.s.merge_cluster(self.req, max_calls=4)
        self.assertEqual(r.status, "failed")
        self.assertFalse(r.events)

    def test_schemas(self):
        from jsonschema import Draft202012Validator

        from llm.cluster_merge.models import MergeResult

        r = self.s.merge_cluster(self.req, max_calls=2)
        Draft202012Validator(MergeResult.model_json_schema()).validate(r.model_dump())

    def test_multistep(self):
        req = self.req.model_copy(
            update={
                "articles": [
                    Article(id=str(i), text=("новость " + str(i) + " ") * 120)
                    for i in range(4)
                ],
                "trigger_id": "0",
            }
        )
        base = len(
            json.dumps(
                messages(req, []),
                ensure_ascii=False,
            )
        )
        req = req.model_copy(update={"profile": Profile(max_input_chars=base + 3800)})
        self.assertGreater(len(plan_merge(req).batches), 1)
        result = self.s.merge_cluster(req, max_calls=20)
        self.assertGreater(self.p.calls, 1)
        self.assertIn(result.status, ("completed", "partial"))
        self.assertEqual(
            sum(len(f.text) for f in plan_merge(req).fragments),
            sum(len(a.text) for a in req.articles),
        )


if __name__ == "__main__":
    unittest.main()
