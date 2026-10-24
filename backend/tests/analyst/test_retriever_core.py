"""Ядро аналитика под нового ретривера: идентичность события, явные исходы отказов,
цитаты, интересы-гипотезы, неразрешённые кандидаты, ключ кэша. Только FakeLLM, без сети."""

from __future__ import annotations

import json
import unittest

from app.analyst import prompts
from app.analyst.analyst import BusinessAnalyst, cache_key
from app.analyst.cache import AnalysisCache
from app.analyst.llm_gateway import BudgetExceeded, BudgetLedger, FakeLLM, LLMUnavailable
from app.analyst.schemas import ExcludedCandidate
from app.analyst.settings import AnalystSettings

from tests.analyst.helpers import bundle_event, draft, make_bundle, analyst_input, seed_company

MODEL = AnalystSettings.from_env({}).model


def fake(responder=None):
    return FakeLLM(responder if responder is not None else (lambda req: json.dumps(draft(), ensure_ascii=False)),
                   ledger=BudgetLedger(100.0))


def two_candidates(**kw):
    company = seed_company()
    events = [
        bundle_event(make_bundle("ev_a", "Событие А про квоты на прокат", [{"text": "ЕС вводит квоты на прокат."}], cluster_id="cl_shared")),
        bundle_event(make_bundle("ev_b", "Событие Б про пошлины на сталь", [{"text": "Россия вводит пошлины на сталь."}], cluster_id="cl_shared")),
    ]
    inp = analyst_input(company, "cfo", events)
    return inp


def with_identity(inp, ids):
    cands = [c.model_copy(update=u) for c, u in zip(inp.candidates, ids)]
    return inp.model_copy(update={"candidates": cands})


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.analyst = BusinessAnalyst(fake(), model=MODEL, max_parallel=1)

    def _run(self, inp):
        out, _ = self.analyst.analyze(inp)
        return out

    def test_old_input_without_canonical_keeps_cluster_dedup(self):
        inp = two_candidates()
        self.assertEqual(inp.candidates[0].cluster_id, inp.candidates[1].cluster_id)
        out = self._run(inp)
        self.assertEqual(len(out.ranked_insights), 1)
        self.assertEqual([e.reason_code for e in out.excluded_candidates], ["duplicate"])

    def test_different_canonical_with_shared_cluster_both_kept(self):
        inp = with_identity(two_candidates(), [
            {"reading_group_id": "rg1", "canonical_event_id": "ce1"},
            {"reading_group_id": "rg1", "canonical_event_id": "ce2"},
        ])
        out = self._run(inp)
        self.assertEqual(len(out.ranked_insights), 2)
        self.assertFalse(out.excluded_candidates)
        self.assertEqual({r.canonical_event_id for r in out.ranked_insights}, {"ce1", "ce2"})
        self.assertEqual({r.reading_group_id for r in out.ranked_insights}, {"rg1"})
        self.assertEqual(out.ranked_insights[0].event_refs[0].canonical_event_id, out.ranked_insights[0].canonical_event_id)
        self.assertTrue(all(c.canonical_event_id for c in out.run_metadata.candidates))

    def test_same_canonical_second_is_duplicate(self):
        inp = with_identity(two_candidates(), [{"canonical_event_id": "ce1"}, {"canonical_event_id": "ce1"}])
        out = self._run(inp)
        self.assertEqual(len(out.ranked_insights), 1)
        self.assertEqual(len(out.excluded_candidates), 1)
        ex = out.excluded_candidates[0]
        self.assertEqual(ex.reason_code, "duplicate")
        self.assertEqual(ex.canonical_event_id, "ce1")
        self.assertEqual(ex.status, "excluded")


class TestOutcomes(unittest.TestCase):
    def test_over_budget_candidate_is_deferred_without_llm_call(self):
        inp = two_candidates()
        inp = with_identity(inp, [
            {"quality_flags": ["context_too_large"], "omitted_parts": ["утверждение x", "цитата y"]},
            {},
        ])
        llm = fake()
        a = BusinessAnalyst(llm, model=MODEL, max_parallel=1)
        out, results = a.analyze(inp)
        ex = {e.candidate_id: e for e in out.excluded_candidates}[inp.candidates[0].candidate_id]
        self.assertEqual(ex.reason_code, "context_too_large")
        self.assertEqual(ex.status, "deferred")
        self.assertEqual(ex.missing_parts, ["утверждение x", "цитата y"])
        self.assertEqual(len(llm.requests), 1)  # только второй кандидат
        self.assertEqual(out.run_metadata.outcome_counts.get("context_too_large"), 1)

    def test_fit_to_budget_marks_over_budget_candidate(self):
        from app.analyst.batch_adapter import ImpactBatchAdapter

        inp = two_candidates()
        adapter = ImpactBatchAdapter(prompt_version="analyst-v1.1", estimator=lambda i, c: 10_000)
        new_inp, report, _ = adapter.fit_to_budget(inp, 500)
        self.assertTrue(all(r["over_budget"] for r in report.values()))
        for c in new_inp.candidates:
            self.assertIn("context_too_large", c.quality_flags)
            self.assertTrue(c.omitted_parts)
        a = BusinessAnalyst(fake(), model=MODEL, max_parallel=1)
        out, _ = a.analyze(new_inp)
        self.assertEqual({e.reason_code for e in out.excluded_candidates}, {"context_too_large"})
        self.assertFalse(out.ranked_insights)

    def test_api_error_is_analysis_failed_not_not_relevant(self):
        llm = fake([LLMUnavailable("шлюз недоступен", code="unavailable")] * 4)
        a = BusinessAnalyst(llm, model=MODEL, max_parallel=1)
        out, results = a.analyze(two_candidates())
        self.assertEqual({e.reason_code for e in out.excluded_candidates}, {"analysis_failed"})
        self.assertEqual({e.status for e in out.excluded_candidates}, {"failed"})
        self.assertNotIn("not_relevant", {e.reason_code for e in out.excluded_candidates})
        self.assertEqual(out.run_metadata.outcome_counts, {"analysis_failed": 2})
        self.assertEqual(len(out.run_metadata.candidates), 2)  # попали в трассу прогона

    def test_budget_exhausted_is_own_outcome(self):
        llm = fake([BudgetExceeded("бюджет")] * 4)
        a = BusinessAnalyst(llm, model=MODEL, max_parallel=1)
        out, _ = a.analyze(two_candidates())
        self.assertEqual({e.reason_code for e in out.excluded_candidates}, {"budget_exhausted"})
        self.assertEqual({e.status for e in out.excluded_candidates}, {"deferred"})

    def test_outcome_codes_valid_and_status_derived(self):
        base = dict(candidate_id="c", cluster_id="k", event_refs=[], reason="r")
        for code, status in [("not_relevant", "excluded"), ("insufficient_evidence", "excluded"), ("analysis_failed", "failed"),
                             ("budget_exhausted", "deferred"), ("context_too_large", "deferred")]:
            self.assertEqual(ExcludedCandidate(reason_code=code, **base).status, status)


class TestBridgeKeepsFailuresOutOfNotRelevant(unittest.TestCase):
    def _mk(self, code):
        from app.analyst.schemas import CandidateOutcome, CandidateRun

        ex = ExcludedCandidate(candidate_id="c", cluster_id="k", event_refs=[], reason_code=code, reason="причина")
        return CandidateOutcome(excluded=ex, run=CandidateRun(candidate_id="c", prompt_version="analyst-v1.0"))

    def test_bridge_does_not_call_failures_irrelevant(self):
        from app.analyst.bridge import relevance_from

        for code in ("analysis_failed", "budget_exhausted", "context_too_large"):
            rel = relevance_from(self._mk(code))
            self.assertTrue(rel.explanation.startswith("Анализ не выполнен"), code)
            self.assertEqual(rel.evidence_status, "not_analyzed")
        self.assertTrue(relevance_from(self._mk("not_relevant")).explanation.startswith("Не влияет"))

    def test_service_flags_non_verdict_outcomes(self):
        from app.analyst.analyst import AnalysisUnavailable
        from app.analyst.service import AnalystService

        self.assertEqual(AnalystService._no_verdict(AnalysisUnavailable("x", "budget_exhausted")), "budget_exhausted")
        self.assertEqual(AnalystService._no_verdict(AnalysisUnavailable("x", "unavailable")), "analysis_failed")
        self.assertEqual(AnalystService._no_verdict(self._mk("context_too_large")), "context_too_large")
        self.assertIsNone(AnalystService._no_verdict(self._mk("not_relevant")))


if __name__ == "__main__":
    unittest.main()
