"""Совместная работа адаптера RetrievalCandidateBatch.v1 и аналитика (цитаты S*, гипотезы, исходы, ключи кэша).

Без сети: FakeLLM. Вход - публичные примеры docs/examples/retriever-analyst и синтетические фикстуры.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from app.analyst import prompts
from app.analyst.analyst import cache_key, input_ref, loose_key
from app.analyst.retrieval_adapter import adapt_retrieval_batch
from app.analyst.view import build_prompt_view
from tests.analyst.helpers import analyst_input, draft, make_service, seed_company, seed_event
from tests.analyst.retrieval_fixtures import batch, contexts, merged, source

EXAMPLES = Path(__file__).resolve().parents[3] / "docs/examples/retriever-analyst"


def example(name):
    return json.loads((EXAMPLES / (name + ".json")).read_text(encoding="utf-8"))


def adapt(b=None, **kw):
    p, i = contexts()
    return adapt_retrieval_batch(b or batch(), p, i, "cfo", **kw)


def view_for(result, version):
    return build_prompt_view(result.analyst_input, result.analyst_input.candidates[0], extras=prompts.supports_extras(version))


class SingleQuoteBlockTests(unittest.TestCase):
    def test_v13_has_source_quotes_once_and_no_original_quotes(self):
        a, b = source("a", "СИНТЕТИКА: тариф вырос на 12 процентов."), source("b", "Второй источник сообщает о росте тарифа.")
        r = adapt(batch([merged([a, b])], [a, b]))
        data = view_for(r, "analyst-v1.3").data
        event = data["event"]
        self.assertNotIn("original_quotes", event)
        self.assertEqual(len(data["source_quotes"]["items"]), 2)
        self.assertEqual(len(event["derived_context"]), 1)  # производный пересказ - один раз
        self.assertIn("source_quotes", event["evidence_policy"])
        self.assertNotIn("original_quotes", event["evidence_policy"])
        # утверждение само является точным фрагментом: текст цитаты не повторяется
        for item in data["source_quotes"]["items"]:
            self.assertTrue(item["same_as_claim"])
            self.assertNotIn("quote", item)
            self.assertIn(item["claim"], {c["id"] for c in event["claims"]})
            self.assertTrue(item["date"] and item["publisher"] and "start" in item and "end" in item)

    def test_older_prompt_versions_keep_original_quotes_for_new_input(self):
        s = source("a", "Текст источника А.")
        r = adapt(batch([merged([s])], [s]))
        for version in ("analyst-v1.0", "analyst-v1.1", "analyst-v1.2"):
            data = view_for(r, version).data
            self.assertEqual(len(data["event"]["original_quotes"]), 1)
            self.assertNotIn("source_quotes", data)
            self.assertIn("original_quotes", data["event"]["evidence_policy"])

    def test_legacy_input_prompt_has_no_retrieval_blocks_in_any_version(self):
        inp = analyst_input(seed_company(), "cfo", [seed_event("e1_key_rate")])
        cand = inp.candidates[0]
        self.assertIsNone(cand.canonical_event_id)
        for version in ("analyst-v1.0", "analyst-v1.1", "analyst-v1.2", "analyst-v1.3"):
            data = build_prompt_view(inp, cand, extras=prompts.supports_extras(version)).data
            text = json.dumps(data, ensure_ascii=False)
            # source_quotes в v1.3 - прежний блок цитат первоисточника; прочего у старого входа нет
            for key in ("original_quotes", "derived_context", "evidence_policy", "hypotheses"):
                self.assertNotIn('"' + key + '"', text, (version, key))
            if version != "analyst-v1.3":
                self.assertNotIn('"source_quotes"', text)


class CacheKeyTests(unittest.TestCase):
    def test_legacy_key_has_no_suffix_and_new_key_carries_both_fingerprints(self):
        inp = analyst_input(seed_company(), "cfo", [seed_event("e1_key_rate")])
        key = cache_key("C", inp.candidates[0], "cfo", "p", "analyst-v1.0", "m")
        self.assertEqual(len(key.split("|")), 6)
        r = adapt()
        cand = r.analyst_input.candidates[0]
        ref = input_ref(r.analyst_input, view_for(r, "analyst-v1.3"))
        self.assertIsNotNone(ref)
        new = cache_key("C", cand, "cfo", "p", "analyst-v1.3", "m", None, ref).split("|")
        self.assertEqual(new[-2], ref)
        self.assertEqual(new[-1], "rc:" + cand.retrieval_context_hash)
        self.assertTrue(loose_key("C", cand, "cfo", "p", "analyst-v1.3", ref).endswith("rc:" + cand.retrieval_context_hash))

    def test_interest_text_change_changes_key(self):
        p, i = contexts()
        k1 = cache_key("C", adapt().analyst_input.candidates[0], "cfo", "p", "v", "m")
        i["interests"][0]["text"] = "Другая гипотеза"
        k2 = cache_key("C", adapt_retrieval_batch(batch(), p, i, "cfo").analyst_input.candidates[0], "cfo", "p", "v", "m")
        self.assertNotEqual(k1, k2)


class OutputIdentityTests(unittest.TestCase):
    def test_all_reading_groups_reach_output_and_membership_is_not_duplicate(self):
        b = batch()
        second = copy.deepcopy(b["candidates"][0])
        second["group_ids"] = ["other-group"]
        b["candidates"].append(second)
        service, fake, _ = make_service()
        p, i = contexts()
        out, adapted = service.analyze_batch(b, "cfo", company_profile=p, active_interests=i)
        self.assertEqual(fake.stats.calls, 1)
        insight = out.ranked_insights[0]
        self.assertEqual(insight.reading_group_ids, ["other-group", "reading-group"])
        self.assertEqual(insight.reading_group_id, "other-group")
        self.assertEqual(insight.event_refs[0].reading_group_ids, ["other-group", "reading-group"])
        self.assertEqual(insight.canonical_event_id, "article:synthetic:article-a")
        self.assertEqual(adapted.candidates[1]["state"], "duplicate")

    def test_adapter_hold_context_too_large_is_explicit_outcome_without_llm(self):
        service, fake, _ = make_service()
        out, adapted = service.analyze_batch(
            example("batch"), "cfo", company_profile=example("company-profile"),
            active_interests=example("active-interests"), analysis_policy={"max_input_tokens": 100},
        )
        self.assertEqual(fake.stats.calls, 0)
        self.assertEqual(out.ranked_insights, [])
        self.assertEqual(len(out.excluded_candidates), 3)
        for e in out.excluded_candidates:
            self.assertEqual((e.reason_code, e.status), ("context_too_large", "deferred"))
            self.assertTrue(e.missing_parts and e.canonical_event_id and e.reading_group_ids)
            self.assertEqual(e.event_refs[0].canonical_event_id, e.canonical_event_id)
        self.assertEqual(out.run_metadata.outcome_counts, {"context_too_large": 3})
        self.assertEqual(adapted.status, "partial")
        self.assertEqual(out.run_metadata.input_diagnostics["status"], "partial")


class EndToEndExampleTests(unittest.TestCase):
    """Пример Егора: batch.json -> адаптер -> FakeLLM -> вывод, v1.3 (цитаты S1, гипотезы)."""

    def test_example_batch_end_to_end_with_quote_alias(self):
        seen = []

        def respond(req):
            seen.append(req.messages[-1]["content"])
            d = draft()
            d["what_happened"] = [{"text": "Опубликован проект изменения тарифа.", "kind": "fact", "claim_ids": ["C1"], "quote_ids": ["S1"]}]
            return json.dumps(d, ensure_ascii=False)

        service, fake, _ = make_service(responder=respond, extra_env={"ANALYST_PROMPT_VERSION": "analyst-v1.3"})
        out, adapted = service.analyze_batch(
            example("batch"), "cfo", company_profile=example("company-profile"), active_interests=example("active-interests"))
        self.assertEqual((adapted.status, len(adapted.analyst_input.candidates), fake.stats.calls), ("ready", 3, 3))
        self.assertEqual(len(out.ranked_insights), 3)
        self.assertNotEqual(out.run_metadata.qa_status, "failed")
        for prompt in seen:
            self.assertEqual(prompt.count('"source_quotes"'), 1)
            self.assertEqual(prompt.count('"original_quotes"'), 0)
            self.assertEqual(prompt.count('"evidence_policy"'), 1)
        for insight in out.ranked_insights:
            st = insight.facts[0]
            self.assertTrue(st.evidence_ids, "алиас S1 должен разрешиться в настоящий evidence_id")
            self.assertTrue(all(e.startswith("quote:") for e in st.evidence_ids))
            self.assertTrue(insight.canonical_event_id and insight.reading_group_ids)
        self.assertEqual(out.run_metadata.input_diagnostics["status"], "ready")


if __name__ == "__main__":
    unittest.main()
