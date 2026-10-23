"""Библиотека few-shot примеров аналитика: схема, ссылки, QA, бюджет, детерминизм."""

from __future__ import annotations

import pytest

from app.analyst import examples as lib
from app.analyst.schemas import AnalystInput, AnalystOutput, LLMDraft

EXAMPLES = lib.load_examples()
REFERENCE = [e for e in EXAMPLES if not e.is_anti]
ANTI = [e for e in EXAMPLES if e.is_anti]
TOPICS = ["rates", "fx", "sanctions", "taxes", "commodities", "inflation", "industry", "other", "policy_rate", "unknown"]


def test_library_shape():
    assert 6 <= len(EXAMPLES) <= 12
    decisions = [e.decision for e in REFERENCE]
    assert decisions.count("exclude") >= 2 and decisions.count("watch") >= 1 and ANTI
    assert any("rates" in e.topics for e in REFERENCE)


@pytest.mark.parametrize("ex", EXAMPLES, ids=lambda e: e.example_id)
def test_schema_ids_and_sources(ex):
    inp = AnalystInput.model_validate(ex.input)
    LLMDraft.model_validate(ex.draft)
    assert inp.request.role == ex.role and len(inp.candidates) == 1
    known = {s["source_id"] for s in lib.load_sources()["sources"]}
    assert set(ex.source_ids) <= known
    bundle = inp.candidates[0].analysis_projection.event_bundles[0]
    ev_ids = {e.evidence_id for e in bundle.evidence}
    for c in bundle.claims:
        assert c.evidence_ids and set(c.evidence_ids) <= ev_ids
    for e in bundle.evidence:
        assert e.source_id in known
    # Псевдонимы в черновике существуют во входе.
    n_f, n_c, n_b = len(inp.company_context.verified_facts), len(bundle.claims), len(inp.candidates[0].bank_fragments)
    for ref in lib._referenced(ex.draft, "F"):
        assert 1 <= int(ref[1:]) <= n_f, ref
    for ref in lib._referenced(ex.draft, "C"):
        assert 1 <= int(ref[1:]) <= n_c, ref
    if not ex.is_anti:
        for ref in ex.draft.get("bank_assessment", {}).get("bank_chunk_ids", []):
            assert 1 <= int(ref[1:]) <= n_b, ref


@pytest.mark.parametrize("ex", REFERENCE, ids=lambda e: e.example_id)
def test_reference_passes_qa_and_output(ex):
    qa, _ = lib.run_qa(ex)
    assert qa.status == "passed", [(i.code, i.message) for i in qa.issues]
    assert qa.decision == ex.decision
    out = AnalystOutput.model_validate(ex.expected_output)
    inp = AnalystInput.model_validate(ex.input)
    if ex.decision == "exclude":
        assert out.excluded_candidates and not out.ranked_insights
        facts = out.excluded_candidates[0].facts
    else:
        ins = out.ranked_insights[0]
        assert ins.decision == ex.decision
        facts = ins.facts
        feats = {f.feature_id for f in inp.company_context.verified_facts}
        assert set(ins.support.company_feature_ids) <= feats and ins.support.company_feature_ids
    claims = {c.claim_id for b in inp.candidates[0].analysis_projection.event_bundles for c in b.claims}
    assert facts and all(set(s.claim_ids) <= claims for s in facts)


@pytest.mark.parametrize("ex", ANTI, ids=lambda e: e.example_id)
def test_anti_examples_explained_and_caught(ex):
    d = ex.defect
    assert d and d["summary"] and d["what_is_wrong"] and d["fix"] and d["bad_fields"]
    qa, _ = lib.run_qa(ex)
    codes = {i.code for i in qa.issues}
    assert set(d["expected_qa_codes"]) <= codes, codes


@pytest.mark.parametrize("topic", TOPICS)
@pytest.mark.parametrize("decision", [None, "include", "watch", "exclude"])
def test_selection_budget_and_determinism(topic, decision):
    sel = lib.select_examples(topic, role="cfo", decision=decision)
    assert 1 <= len(sel) <= 2
    assert not any(e.is_anti for e in sel)
    block = lib.format_examples_block(sel)
    assert lib.estimate_tokens(block) <= 1200
    assert "не факты о текущей компании" in block
    assert [e.example_id for e in lib.select_examples(topic, role="cfo", decision=decision)] == [e.example_id for e in sel]
    if decision in ("watch", "exclude"):
        assert any(e.decision == "exclude" for e in sel)


def test_max_n_and_anti_flag():
    assert lib.select_examples("rates", max_n=0) == []
    assert len(lib.select_examples("rates", max_n=1, max_tokens=5000)) == 1
    sel = lib.select_examples("rates", max_n=3, max_tokens=5000, include_anti=True)
    assert len(sel) <= 3 and any(e.is_anti for e in sel)
    assert "так НЕ отвечать" in lib.format_examples_block(sel)
