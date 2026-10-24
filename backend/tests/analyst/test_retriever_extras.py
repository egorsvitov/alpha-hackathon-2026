"""Цитаты первоисточника, интересы-гипотезы, неразрешённые кандидаты и ключ кэша (analyst-v1.3).
Только FakeLLM и кэш в памяти, без сети."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.analyst import prompts
from app.analyst.analyst import BusinessAnalyst, cache_key, input_ref
from app.analyst.cache import AnalysisCache
from app.analyst.llm_gateway import BudgetLedger, FakeLLM
from app.analyst.qa import validate_draft
from app.analyst.schemas import AnalysisPolicy, LLMDraft, RetrievalInterest
from app.analyst.settings import AnalystSettings
from app.analyst.view import QUOTE_TOKEN_LIMIT, build_prompt_view

from tests.analyst.helpers import analyst_input, bundle_event, draft, make_bundle, seed_company

MODEL = AnalystSettings.from_env({}).model
V13 = prompts.PROMPT_VERSION_V13


def fake():
    return FakeLLM(lambda req: json.dumps(draft(), ensure_ascii=False), ledger=BudgetLedger(100.0))


def quote_bundle(event_id="evq", n_claims=1, quote_len=0, dated="2026-10-20"):
    claims = [{"text": f"Утверждение номер {i} про квоты на прокат."} for i in range(1, n_claims + 1)]
    b = make_bundle(event_id, "Событие с цитатами", claims)
    for i, e in enumerate(b["evidence"], start=1):
        e["quote"] = f"Дословная цитата {i} из первоисточника про квоты. " + ("Подробность. " * quote_len)
        e["start"], e["end"] = 10 * i, 10 * i + len(e["quote"])
    b["sources"][0]["published_at"] = dated
    b["sources"][0]["publisher"] = "Издание Н"
    return b


def one_input(bundle=None, **cand_update):
    inp = analyst_input(seed_company(), "cfo", [bundle_event(bundle or quote_bundle())])
    cands = [c.model_copy(update=cand_update) for c in inp.candidates]
    return inp.model_copy(update={"candidates": cands})


def with_interests(inp, *items):
    ctx = inp.company_context.model_copy(update={"retrieval_interests": [RetrievalInterest(**i) for i in items]})
    return inp.model_copy(update={"company_context": ctx})


class TestSourceQuotes(unittest.TestCase):
    def test_block_has_alias_source_date_coordinates_and_derived_note(self):
        inp = one_input(derived_text=True)
        view = build_prompt_view(inp, inp.candidates[0], extras=True)
        block = view.data["source_quotes"]
        item = block["items"][0]
        self.assertEqual((item["id"], item["claim"], item["date"], item["publisher"]), ("S1", "C1", "2026-10-20", "Издание Н"))
        self.assertEqual(item["source_id"], "evq:src")
        self.assertIn("start", item)
        self.assertIn("end", item)
        self.assertIn("Дословная цитата 1", item["quote"])
        self.assertIn("производный пересказ", block["note"])
        self.assertTrue(view.data["event"]["derived_text"])

    def test_old_view_and_old_prompts_have_no_quote_block(self):
        inp = one_input()
        view = build_prompt_view(inp, inp.candidates[0])
        self.assertNotIn("source_quotes", view.data)
        msgs = prompts.build_analyst_messages(view.data, version=prompts.PROMPT_VERSION_V12)
        self.assertNotIn("source_quotes", json.dumps(msgs, ensure_ascii=False))
        for v in (prompts.PROMPT_VERSION, prompts.PROMPT_VERSION_V11, prompts.PROMPT_VERSION_V12):
            self.assertFalse(prompts.supports_extras(v))
            self.assertNotIn("source_quotes", prompts.SYSTEM_PROMPTS[v])
        self.assertTrue(prompts.supports_extras(V13))

    def test_token_limit_drops_whole_quotes_and_lists_them(self):
        inp = one_input(quote_bundle(n_claims=12, quote_len=60))
        view = build_prompt_view(inp, inp.candidates[0], extras=True)
        items = view.data["source_quotes"]["items"]
        self.assertTrue(0 < len(items) < 12)
        size = sum(len(json.dumps(i, ensure_ascii=False).encode()) for i in items) // 4
        self.assertLessEqual(size, QUOTE_TOKEN_LIMIT)
        full = {e.quote for e in view.evidence.values()}
        self.assertTrue(all(i["quote"] in full for i in items))  # цитаты не режутся
        self.assertEqual(view.data["source_quotes"]["omitted"], 12 - len(items))
        self.assertTrue(any("опущены целиком" in n for n in view.notes))
        qa = validate_draft(LLMDraft.model_validate(draft()), view, "cfo")
        self.assertTrue(any("опущены целиком" in x for x in qa.limitations))

    def test_matched_claims_keep_quotes_first(self):
        bundle = quote_bundle(n_claims=12, quote_len=60)
        inp = one_input(bundle)
        cand = inp.candidates[0]
        last = bundle["claims"][-1]["claim_id"]
        cand = cand.model_copy(update={"match_context": cand.match_context.model_copy(update={"matched_event_claim_ids": [last]})})
        view = build_prompt_view(inp, cand, extras=True)
        self.assertIn("C12", {i["claim"] for i in view.data["source_quotes"]["items"]})

    def test_qa_checks_quote_aliases(self):
        inp = one_input()
        view = build_prompt_view(inp, inp.candidates[0], extras=True)
        d = draft(
            what_happened=[{"text": "Источник сообщает о квотах.", "kind": "fact", "claim_ids": ["C1"], "quote_ids": ["S1"]}],
            risks=[{"text": "Возможен рост издержек.", "kind": "hypothesis", "feature_ids": ["F1"], "quote_ids": ["S9"]}],
        )
        qa = validate_draft(LLMDraft.model_validate(d), view, "cfo")
        self.assertEqual(qa.facts[0].evidence_ids, ["evq:ev1"])
        self.assertTrue(any(i.code == "unknown_ref" and "S9" in i.message for i in qa.issues))
        # В кэше хранятся исходные ID: псевдоним S1 → evidence_id, и обратно он находится.
        real = view.realias_draft(LLMDraft.model_validate(d).model_dump(mode="json"))
        self.assertEqual(real["what_happened"][0]["quote_ids"], ["evq:ev1"])
        qa2 = validate_draft(LLMDraft.model_validate(real), view, "cfo")
        self.assertEqual(qa2.facts[0].evidence_ids, ["evq:ev1"])

    def test_v13_prompt_gets_quotes_end_to_end(self):
        llm = fake()
        a = BusinessAnalyst(llm, model=MODEL, prompt_version=V13, max_parallel=1)
        out, _ = a.analyze(one_input())
        self.assertEqual(len(out.ranked_insights), 1)
        self.assertIn('"source_quotes"', llm.requests[0].messages[-1]["content"])
        self.assertIn("quote_ids", llm.requests[0].messages[0]["content"])


class TestInterests(unittest.TestCase):
    ITEMS = ({"interest_id": "int-1", "text": "Спрос на прокат может зависеть от квот ЕС", "version": "v1"},)

    def test_shown_as_separate_hypothesis_block(self):
        inp = with_interests(one_input(), *self.ITEMS)
        view = build_prompt_view(inp, inp.candidates[0], extras=True)
        block = view.data["company"]["possible_impact_channels"]
        self.assertIn("гипотезы", block["note"])
        self.assertEqual(block["items"][0]["id"], "H1")
        self.assertNotIn("possible_impact_channels", build_prompt_view(inp, inp.candidates[0]).data["company"])
        self.assertEqual(inp.company_context.retrieval_interests[0].kind, "hypothesis")
        self.assertIn("гипотезы ретривера", prompts.ANALYST_SYSTEM_PROMPT_V13)

    def test_statement_resting_only_on_interest_cannot_be_fact(self):
        inp = with_interests(one_input(), *self.ITEMS)
        view = build_prompt_view(inp, inp.candidates[0], extras=True)
        d = draft(
            risks=[{"text": "Канал через спрос на прокат.", "kind": "fact", "interest_ids": ["H1"]}],
            opportunities=[{"text": "Тот же канал, но с утверждением источника.", "kind": "inference", "interest_ids": ["H1"], "claim_ids": ["C1"]}],
        )
        qa = validate_draft(LLMDraft.model_validate(d), view, "cfo")
        self.assertEqual(qa.risks[0].kind, "hypothesis")
        self.assertEqual(qa.risks[0].interest_ids, ["int-1"])
        self.assertTrue(any(i.code == "interest_as_fact" for i in qa.issues))
        self.assertEqual(qa.opportunities[0].kind, "inference")

    def test_interest_alias_without_block_is_unknown_ref(self):
        inp = one_input()
        view = build_prompt_view(inp, inp.candidates[0], extras=True)
        d = draft(risks=[{"text": "Канал.", "kind": "fact", "interest_ids": ["H1"]}])
        qa = validate_draft(LLMDraft.model_validate(d), view, "cfo")
        self.assertTrue(any(i.code == "unknown_ref" for i in qa.issues))
        self.assertFalse(qa.risks)


class TestCompleteness(unittest.TestCase):
    def test_unresolved_analyses_best_document_with_constraint_and_limitation(self):
        inp = one_input(completeness="unresolved", group_member_refs=["a", {"event_id": "b"}, "c"])
        llm = fake()
        a = BusinessAnalyst(llm, model=MODEL, prompt_version=V13, max_parallel=1)
        out, _ = a.analyze(inp)
        self.assertEqual(len(llm.requests), 1)
        user = llm.requests[0].messages[-1]["content"]
        self.assertIn('"completeness"', user)
        self.assertIn("не означает, что все материалы описывают одно событие", user)
        self.assertIn("completeness", llm.requests[0].messages[0]["content"])
        ins = out.ranked_insights[0]
        self.assertTrue(any("Данные неполны" in x and "лучший" in x for x in ins.support.limitations))

    def test_complete_and_missing_have_no_block_or_limitation(self):
        for v in ("complete", None):
            inp = one_input(completeness=v)
            view = build_prompt_view(inp, inp.candidates[0], extras=True)
            self.assertNotIn("completeness", view.data["event"])
            self.assertFalse(view.notes)


class TestCacheKey(unittest.TestCase):
    def key(self, inp, extras=True):
        cand = inp.candidates[0]
        ref = input_ref(inp, build_prompt_view(inp, cand, extras=extras))
        return ref, cache_key("C", cand, "cfo", "p1", V13, MODEL, None, ref)

    def test_old_input_keeps_exact_old_key(self):
        inp = analyst_input(seed_company(), "cfo", [bundle_event(make_bundle("e1", "Т", [{"text": "Текст события."}]))])
        cand = inp.candidates[0]
        ref, key = self.key(inp, extras=False)
        self.assertIsNone(ref)
        self.assertEqual(key, cache_key("C", cand, "cfo", "p1", V13, MODEL))
        self.assertEqual(len(key.split("|")), 6)

    def test_interest_text_version_quotes_policy_change_key(self):
        base = one_input()
        a = with_interests(base, {"interest_id": "i", "text": "Один текст", "version": "v1"})
        b = with_interests(base, {"interest_id": "i", "text": "Другой текст", "version": "v1"})
        c = with_interests(base, {"interest_id": "i", "text": "Один текст", "version": "v2"})
        self.assertEqual(len({self.key(x)[1] for x in (base, a, b, c)}), 4)
        same = with_interests(base, {"interest_id": "i", "text": "Один текст", "version": "v1"})
        self.assertEqual(self.key(a)[1], self.key(same)[1])
        self.assertNotEqual(self.key(base)[1], self.key(one_input(quote_bundle(dated="2026-10-19")))[1])
        pol = base.model_copy(update={"analysis_policy": AnalysisPolicy(do_not_infer_supplier_from_payment=False)})
        self.assertNotEqual(self.key(base)[1], self.key(pol)[1])
        # Для версий промпта без extras интересы в ключ не входят (в модель они не попадают).
        self.assertEqual(self.key(base, extras=False)[1], self.key(a, extras=False)[1])

    def test_changed_interest_text_forces_new_llm_call_and_same_text_hits_cache(self):
        llm = fake()
        a = BusinessAnalyst(llm, model=MODEL, prompt_version=V13, cache=AnalysisCache(None, None), max_parallel=1)
        base = one_input()
        i1 = with_interests(base, {"interest_id": "i", "text": "Один текст"})
        i2 = with_interests(base, {"interest_id": "i", "text": "Другой текст"})
        a.analyze(i1)
        a.analyze(i1)
        self.assertEqual(len(llm.requests), 1)
        a.analyze(i2)
        self.assertEqual(len(llm.requests), 2)

    def test_all_bundled_answers_are_found_by_their_old_keys(self):
        bundled = Path(__file__).resolve().parents[2] / "app" / "analyst" / "cache"
        cache = AnalysisCache(None, bundled)
        found = total = 0
        for path in sorted(bundled.rglob("*.json")):
            env = json.loads(path.read_text(encoding="utf-8"))
            total += 1
            rec, src = cache.get(env["key"])
            found += int(rec is not None and src == "bundled")
        self.assertEqual((found, total), (84, 84))


if __name__ == "__main__":
    unittest.main()
