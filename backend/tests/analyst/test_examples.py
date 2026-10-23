"""Библиотека few-shot примеров аналитика: схема, ссылки, QA, бюджет, детерминизм."""

from __future__ import annotations

import unittest

from app.analyst import examples as lib
from app.analyst.schemas import AnalystInput, AnalystOutput, LLMDraft

EXAMPLES = lib.load_examples()
REFERENCE = [e for e in EXAMPLES if not e.is_anti]
ANTI = [e for e in EXAMPLES if e.is_anti]
TOPICS = ["rates", "fx", "sanctions", "taxes", "commodities", "inflation", "industry", "other", "policy_rate", "unknown"]


class TestExamples(unittest.TestCase):
    def test_library_shape(self) -> None:
        self.assertTrue(6 <= len(EXAMPLES) <= 12)
        decisions = [e.decision for e in REFERENCE]
        self.assertTrue(decisions.count("exclude") >= 2 and decisions.count("watch") >= 1 and ANTI)
        self.assertTrue(any("rates" in e.topics for e in REFERENCE))

    def test_schema_ids_and_sources(self) -> None:
        known = {s["source_id"] for s in lib.load_sources()["sources"]}
        for ex in EXAMPLES:
            with self.subTest(example_id=ex.example_id):
                inp = AnalystInput.model_validate(ex.input)
                LLMDraft.model_validate(ex.draft)
                self.assertEqual(inp.request.role, ex.role)
                self.assertEqual(len(inp.candidates), 1)
                self.assertTrue(set(ex.source_ids) <= known)
                bundle = inp.candidates[0].analysis_projection.event_bundles[0]
                ev_ids = {e.evidence_id for e in bundle.evidence}
                for c in bundle.claims:
                    self.assertTrue(c.evidence_ids and set(c.evidence_ids) <= ev_ids)
                for e in bundle.evidence:
                    self.assertIn(e.source_id, known)
                # Псевдонимы в черновике существуют во входе.
                n_f = len(inp.company_context.verified_facts)
                n_c = len(bundle.claims)
                n_b = len(inp.candidates[0].bank_fragments)
                for ref in lib._referenced(ex.draft, "F"):
                    self.assertTrue(1 <= int(ref[1:]) <= n_f, ref)
                for ref in lib._referenced(ex.draft, "C"):
                    self.assertTrue(1 <= int(ref[1:]) <= n_c, ref)
                if not ex.is_anti:
                    for ref in ex.draft.get("bank_assessment", {}).get("bank_chunk_ids", []):
                        self.assertTrue(1 <= int(ref[1:]) <= n_b, ref)

    def test_reference_passes_qa_and_output(self) -> None:
        for ex in REFERENCE:
            with self.subTest(example_id=ex.example_id):
                qa, _ = lib.run_qa(ex)
                self.assertEqual(qa.status, "passed", [(i.code, i.message) for i in qa.issues])
                self.assertEqual(qa.decision, ex.decision)
                out = AnalystOutput.model_validate(ex.expected_output)
                inp = AnalystInput.model_validate(ex.input)
                if ex.decision == "exclude":
                    self.assertTrue(out.excluded_candidates and not out.ranked_insights)
                    facts = out.excluded_candidates[0].facts
                else:
                    ins = out.ranked_insights[0]
                    self.assertEqual(ins.decision, ex.decision)
                    facts = ins.facts
                    feats = {f.feature_id for f in inp.company_context.verified_facts}
                    self.assertTrue(set(ins.support.company_feature_ids) <= feats and ins.support.company_feature_ids)
                claims = {c.claim_id for b in inp.candidates[0].analysis_projection.event_bundles for c in b.claims}
                self.assertTrue(facts and all(set(s.claim_ids) <= claims for s in facts))

    def test_anti_examples_explained_and_caught(self) -> None:
        for ex in ANTI:
            with self.subTest(example_id=ex.example_id):
                d = ex.defect
                self.assertTrue(d and d["summary"] and d["what_is_wrong"] and d["fix"] and d["bad_fields"])
                qa, _ = lib.run_qa(ex)
                codes = {i.code for i in qa.issues}
                self.assertTrue(set(d["expected_qa_codes"]) <= codes, codes)

    def test_selection_budget_and_determinism(self) -> None:
        for topic in TOPICS:
            for decision in [None, "include", "watch", "exclude"]:
                with self.subTest(topic=topic, decision=decision):
                    sel = lib.select_examples(topic, role="cfo", decision=decision)
                    self.assertTrue(1 <= len(sel) <= 2)
                    self.assertFalse(any(e.is_anti for e in sel))
                    block = lib.format_examples_block(sel)
                    self.assertTrue(lib.estimate_tokens(block) <= 1200)
                    self.assertIn("не факты о текущей компании", block)
                    self.assertEqual(
                        [e.example_id for e in lib.select_examples(topic, role="cfo", decision=decision)],
                        [e.example_id for e in sel],
                    )
                    if decision in ("watch", "exclude"):
                        self.assertTrue(any(e.decision == "exclude" for e in sel))

    def test_max_n_and_anti_flag(self) -> None:
        self.assertEqual(lib.select_examples("rates", max_n=0), [])
        self.assertEqual(len(lib.select_examples("rates", max_n=1, max_tokens=5000)), 1)
        sel = lib.select_examples("rates", max_n=3, max_tokens=5000, include_anti=True)
        self.assertTrue(len(sel) <= 3 and any(e.is_anti for e in sel))
        self.assertIn("так НЕ отвечать", lib.format_examples_block(sel))


if __name__ == "__main__":
    unittest.main()
