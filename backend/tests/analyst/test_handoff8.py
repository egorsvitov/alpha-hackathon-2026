"""Адаптер ``ImpactCandidateBatch v1`` → ``AnalystInput`` (batch_adapter.py) и прогон на FakeLLM.

Синтетические пакеты строятся в тесте. Реальные пакеты ретривера (8 компаний) НЕ лежат
в репозитории: путь берётся из ``ANALYST_HANDOFF_DIR`` (по умолчанию
``C:/HAKATON/ALPHA/analyst-handoff-8-companies-20261022``); если папки нет — тесты пропускаются.
"""

from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path

from app.analyst import prompts
from app.analyst.analyst import BusinessAnalyst
from app.analyst.batch_adapter import BatchValidationError, ImpactBatchAdapter
from app.analyst.jsonschema_lite import SchemaValidator
from app.analyst.llm_gateway import ChatRequest, FakeLLM

from tests.analyst.helpers import draft, exclude_draft

HANDOFF_DIR = Path(os.environ.get("ANALYST_HANDOFF_DIR", "C:/HAKATON/ALPHA/analyst-handoff-8-companies-20261022"))
HAS_HANDOFF = (HANDOFF_DIR / "analyst-inputs").is_dir()
RESULTS = Path(__file__).resolve().parents[2] / "app" / "analyst" / "eval" / "handoff8_results.json"
V11 = prompts.PROMPT_VERSION_V11
CID = "T_1"


def _claim(eid: str, n: int, text: str) -> dict:
    return {
        "action": None, "attribution": None, "claim_id": f"{eid}:claim{n}", "evidence_ids": [f"{eid}:ev{n}"],
        "field_evidence": {"/text": [f"{eid}:ev{n}"]}, "geographies": [], "measurements": [], "modality": "unknown",
        "object": None, "participants": [], "polarity": "unknown", "scenario": None, "stage": None, "text": text,
        "time": {"effective_at": None, "forecast_horizon": None, "observation_period": None, "occurred_at": None},
    }


def make_candidate(eid: str, title: str, texts: list[str], *, published: str = "2026-10-21T10:00:00+00:00",
                   claim_refs: list[int] | None = None, source_published: str | None = None) -> dict:
    claims = [_claim(eid, i, t) for i, t in enumerate(texts, start=1)]
    evidence = [{"evidence_id": f"{eid}:ev{i}", "quote": t, "source_id": f"{eid}:src", "start": 0, "end": len(t), "field": "text"}
                for i, t in enumerate(texts, start=1)]
    bundle = {
        "schema_version": "0.2", "claims": claims, "conflicts": [], "relations": [], "evidence": evidence,
        "event": {"availability": "active", "event_id": eid, "event_type": "other", "event_version": 1,
                  "first_published_at": published, "quality": "partial", "title": title, "updated_at": published},
        "limitations": ["Embedding benchmark: quote-only deterministic packaging, NOT LLM fact extraction."],
        "ranking_features": {"factors": [], "geographies": [], "industries": [], "metrics": [], "objects": [], "topics": ["other"]},
        "sources": [{"article_id": eid, "article_version": 1, "content_kind": "original", "is_full_original_text": False,
                     "is_synthetic": True, "published_at": source_published or published, "publisher": "test",
                     "source_id": f"{eid}:src", "title": title, "url": "https://example.org/x"}],
    }
    return {
        "candidate_id": (eid.encode().hex() * 8)[:64].ljust(64, "0"),
        "event_cluster_id": None, "cluster_version": None,
        "members": [{"snapshot": {"event_id": eid, "event_version": 1, "payload_sha256": "0" * 64,
                                  "received_at": published, "source_mode": "fixture", "ingest_id": "i" * 8},
                     "bundle": bundle}],
        "match_context": {"channels": ["dense"], "matched_views": ["business"],
                          "feature_ids": [f"{CID}:/okved/name", f"{CID}:/industry"], "neighbor_feature_refs": [],
                          "claim_refs": [{"claim_id": f"{eid}:claim{i}", "event_id": eid, "event_version": 1} for i in (claim_refs or [])],
                          "event_paths": ["/event/title"], "hypotheses": [], "unknowns": []},
        "limitations": ["candidate_requires_business_analysis"],
    }


def _fact(path: str, value) -> dict:
    return {"feature_id": f"{CID}:{path}", "path": path, "value": value, "quality": "observed",
            "origin": {"kind": "csv", "source_path": "test.csv"}}


def make_batch(candidates: list[dict] | None = None, *, status: str = "ready", empty_reason=None, role: str = "cfo") -> dict:
    return {
        "schema_version": "impact_candidate_batch.v1",
        "request": {"request_id": "test:T_1", "company_id": CID, "role": role, "as_of": "2026-10-22T00:00:00+00:00",
                    "window_start": "2026-10-20T00:00:00+00:00", "window_end": "2026-10-22T00:00:00+00:00",
                    "requested_limit": 20, "max_input_tokens": 8192},
        "status": status, "empty_reason": empty_reason,
        "company_context": {
            "profile_version": "pv1", "profile_hash": "a" * 64,
            "snapshot": {"historical_applicability": "unknown"},
            "verified_facts": [_fact("/okved/name", "Строительство жилых и нежилых зданий"), _fact("/products/loan", True),
                               _fact("/products/deposit", False), _fact("/revenue/raw", "1575233.0"),
                               _fact("/company_id", CID), _fact("/fts_present", False)],
            "derived_exposures": [], "group_context": {"status": "not_loaded"}, "payment_context": {"status": "not_loaded"},
            "unknowns": [f"{CID}:/countries", f"{CID}:/revenue/unit"],
        },
        "candidates": candidates if candidates is not None else [
            make_candidate("ev:a", "Банк России сохранил ключевую ставку", ["Банк России сохранил ключевую ставку на уровне 17%."]),
        ],
        "retrieval_context": {"trace_id": "t", "index_snapshot_id": "i", "encoder_fingerprint": "e", "representation_version": "r",
                              "policy_version": "p", "data_watermark": "2026-10-22T00:00:00+00:00", "failed_channels": []},
        "limitations": ["retrieval_similarity_is_not_business_significance"],
    }


class TestBatchValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.ad = ImpactBatchAdapter(prompt_version=V11)

    def test_valid_batch_maps_profile_and_unknowns(self):
        res = self.ad.to_analyst_input(make_batch())
        inp = res.analyst_input
        facts = {f.field: f for f in inp.company_context.verified_facts}
        self.assertNotIn("/company_id", facts)
        self.assertEqual(facts["/products/loan"].value, "есть")
        self.assertEqual(facts["/products/deposit"].value, "нет")
        self.assertIn("в Альфа-Банке", facts["/products/loan"].description)
        self.assertIn("сумму не использовать", facts["/revenue/raw"].description)
        unknowns = " ".join(inp.company_context.unknowns)
        self.assertIn("Платёжный контекст не загружен", unknowns)
        self.assertIn("не значит, что их нет", unknowns)
        self.assertIn("Страны ВЭД неизвестны", unknowns)
        self.assertEqual([e.exposure_id for e in inp.company_context.economic_exposures], ["exp.loans"])
        cand = inp.candidates[0]
        # /industry нет среди проверенных фактов — не основание, а пометка.
        self.assertEqual(cand.match_context.matched_company_feature_ids, [f"{CID}:/okved/name"])
        self.assertTrue(any("/industry" in a for a in cand.match_context.assumptions_not_verified))
        self.assertEqual((inp.request.news_window_hours, inp.request.max_insights), (48, 20))
        self.assertEqual(cand.bank_fragments, [])

    def test_any_role_by_parameter(self):
        for role in ("ceo", "cfo", "treasurer", "chief_accountant"):
            res = self.ad.to_analyst_input(make_batch(), role=role)
            self.assertEqual(res.analyst_input.request.role, role)
        with self.assertRaises(BatchValidationError):
            self.ad.to_analyst_input(make_batch(), role="cto")

    def test_structural_errors_raise(self):
        bad = make_batch()
        del bad["request"]["as_of"]
        with self.assertRaises(BatchValidationError):
            self.ad.to_analyst_input(bad)
        bad = make_batch()
        bad["schema_version"] = "impact_candidate_batch.v2"
        with self.assertRaises(BatchValidationError):
            self.ad.to_analyst_input(bad)
        bad = make_batch()
        bad["company_context"]["verified_facts"].append(dict(_fact("/region", "Москва"), feature_id="OTHER:/region"))
        with self.assertRaises(BatchValidationError):
            self.ad.to_analyst_input(bad)

    def test_bad_candidate_is_rejected_others_kept(self):
        good = make_candidate("ev:good", "Хорошее событие", ["Текст хорошего события достаточной длины."])
        broken = make_candidate("ev:bad", "Плохое событие", ["Текст плохого события."])
        broken["members"][0]["bundle"]["claims"][0]["evidence_ids"] = ["ev:bad:missing"]
        dup = copy.deepcopy(good)
        res = self.ad.to_analyst_input(make_batch([good, broken, dup]))
        self.assertEqual([c.candidate_id for c in res.analyst_input.candidates], [good["candidate_id"]])
        self.assertEqual(len(res.rejected), 2)
        self.assertTrue(any("несуществующие цитаты" in r["reason"] for r in res.rejected))
        self.assertTrue(any("повтор candidate_id" in r["reason"] for r in res.rejected))

    def test_empty_and_degraded_status(self):
        res = self.ad.to_analyst_input(make_batch([], status="empty", empty_reason="no_matches"))
        self.assertTrue(res.is_empty)
        self.assertEqual((res.status, res.empty_reason), ("empty", "no_matches"))
        res = self.ad.to_analyst_input(make_batch(status="empty", empty_reason="budget_exhausted"))
        self.assertTrue(res.is_empty)  # кандидаты при status=empty игнорируются
        self.assertTrue(res.notes)
        res = self.ad.to_analyst_input(make_batch([]))
        self.assertEqual((res.status, res.empty_reason), ("ready", "no_matches"))
        deg = make_batch(status="degraded")
        deg["retrieval_context"]["failed_channels"] = ["lexical"]
        res = self.ad.to_analyst_input(deg)
        self.assertTrue(any("деградированном" in u for u in res.analyst_input.company_context.unknowns))

    def test_empty_batch_gives_empty_output_without_llm(self):
        fake = FakeLLM(lambda req: json.dumps(draft()))
        analyst = BusinessAnalyst(fake, model="z-ai/glm-5.3-flash", prompt_version=V11)
        res = self.ad.to_analyst_input(make_batch([], status="empty", empty_reason="no_events"))
        out, _ = analyst.analyze(res.analyst_input)
        self.assertEqual((out.ranked_insights, out.excluded_candidates, len(fake.requests)), ([], [], 0))

    def test_sources_after_as_of_are_dropped(self):
        cand = make_candidate("ev:late", "Событие", ["Первое утверждение источника."], source_published="2026-10-23T00:00:00+00:00")
        res = self.ad.to_analyst_input(make_batch([cand]))
        b = res.analyst_input.candidates[0].analysis_projection.event_bundles[0]
        self.assertEqual((b.claims, b.sources, b.evidence), ([], [], []))

    def test_unreadable_claims_excluded_without_llm(self):
        cand = make_candidate("ev:moj", "Гол в матче", ["������. 21 �������. INTERFAX.RU - ���������� ��������� �� ������ 4:2"])
        res = self.ad.to_analyst_input(make_batch([cand]))
        self.assertEqual(res.analyst_input.candidates[0].quality_flags, ["unreadable_claims"])
        fake = FakeLLM(lambda req: json.dumps(draft()))
        out, _ = BusinessAnalyst(fake, model="z-ai/glm-5.3-flash", prompt_version=V11).analyze(res.analyst_input)
        self.assertEqual(out.excluded_candidates[0].reason_code, "unreadable_source")
        self.assertEqual(len(fake.requests), 0)


class TestBudgetTruncation(unittest.TestCase):
    def test_drops_whole_low_priority_claims_and_keeps_refs(self):
        texts = [f"Утверждение номер {i}: " + ("подробности события и комментарии участников рынка. " * 6) for i in range(1, 13)]
        cand = make_candidate("ev:big", "Большое событие", texts, claim_refs=[12])
        ad = ImpactBatchAdapter(prompt_version=V11, token_budget=5200)
        res = ad.to_analyst_input(make_batch([cand]))
        c = res.analyst_input.candidates[0]
        rep = res.truncation[c.candidate_id]
        self.assertGreater(rep["tokens_before"], 5200)
        self.assertLessEqual(rep["tokens_after"], 5200)
        self.assertTrue(rep["dropped_claim_ids"])
        b = c.analysis_projection.event_bundles[0]
        kept = {x.claim_id for x in b.claims}
        self.assertIn("ev:big:claim12", kept)  # из claim_refs — защищено
        original = {f"ev:big:claim{i}": t for i, t in enumerate(texts, start=1)}
        evidence = {e.evidence_id: e.quote for e in b.evidence}
        for x in b.claims:
            self.assertEqual(x.text, original[x.claim_id])  # без обрезки
            for ev in x.evidence_ids:
                self.assertEqual(evidence[ev], original[x.claim_id])  # цитата и её ID на месте
        self.assertTrue(set(rep["dropped_evidence_ids"]).isdisjoint(evidence))
        self.assertTrue(any("опущено утверждений" in lim for lim in b.limitations))

    def test_qa_drops_refs_not_in_input(self):
        res = ImpactBatchAdapter(prompt_version=V11).to_analyst_input(make_batch())
        bad = draft(company_relevance={"text": "Связь через признак.", "kind": "inference", "feature_ids": ["F99"], "claim_ids": ["C9"]})
        fake = FakeLLM(lambda req: json.dumps(bad, ensure_ascii=False))
        out, _ = BusinessAnalyst(fake, model="z-ai/glm-5.3-flash", prompt_version=V11).analyze(res.analyst_input)
        ins = out.ranked_insights[0]
        known_f = {f.feature_id for f in res.analyst_input.company_context.verified_facts}
        known_c = {c.claim_id for c in res.analyst_input.candidates[0].analysis_projection.event_bundles[0].claims}
        self.assertTrue(set(ins.support.company_feature_ids) <= known_f)
        self.assertTrue(set(ins.support.event_claim_ids) <= known_c)
        codes = {i.code for r in out.run_metadata.candidates for i in r.qa_issues}
        self.assertIn("unknown_ref", codes)


class TestJsonSchemaLite(unittest.TestCase):
    def test_subset_keywords(self):
        schema = {"$defs": {"P": {"type": "string", "pattern": "^/"}},
                  "type": "object", "additionalProperties": False, "required": ["a", "b"],
                  "properties": {"a": {"type": "integer", "minimum": 1}, "b": {"type": "array", "maxItems": 1, "items": {"$ref": "#/$defs/P"}},
                                 "c": {"anyOf": [{"type": "null"}, {"enum": ["x"]}]}, "d": {"type": "string", "format": "date-time"}}}
        v = SchemaValidator(schema)
        self.assertEqual(v.errors({"a": 1, "b": ["/x"], "c": None, "d": "2026-10-22T00:00:00+00:00"}), [])
        errs = v.errors({"a": 0, "b": ["x", "/y"], "c": "z", "d": "вчера", "e": 1})
        joined = " ".join(errs)
        for needle in ("меньше 1", "элементов больше 1", "шаблону", "anyOf", "дата-время", "лишнее поле «e»"):
            self.assertIn(needle, joined)


@unittest.skipUnless(HAS_HANDOFF, f"нет папки пакетов ретривера ({HANDOFF_DIR}); задайте ANALYST_HANDOFF_DIR")
class TestHandoffEightCompanies(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        schema_dir = HANDOFF_DIR / "schemas"
        cls.ad = ImpactBatchAdapter(prompt_version=V11, reserve_tokens=600, schema_dir=schema_dir if schema_dir.is_dir() else None)
        cls.batches = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((HANDOFF_DIR / "analyst-inputs").glob("*.json"))}

    def test_all_batches_valid_and_within_budget(self):
        self.assertEqual(len(self.batches), 8)
        for cid, raw in self.batches.items():
            res = self.ad.to_analyst_input(raw)
            self.assertEqual(res.rejected, [], cid)
            self.assertEqual(len(res.analyst_input.candidates), 6, cid)
            self.assertTrue(res.schema_checked)
            for cand_id, tokens in res.token_estimates.items():
                self.assertLessEqual(tokens, 8000 - 600, (cid, cand_id))
                self.assertEqual(res.truncation[cand_id]["dropped_claim_ids"], [])
            # Все ID, на которые может сослаться аналитик, существуют во входе.
            for cand in res.analyst_input.candidates:
                b = cand.analysis_projection.event_bundles[0]
                ev_ids = {e.evidence_id for e in b.evidence}
                for c in b.claims:
                    self.assertTrue(set(c.evidence_ids) <= ev_ids)

    def test_other_role_and_unreadable_detection(self):
        raw = self.batches["U_100044"]
        res = self.ad.to_analyst_input(raw, role="chief_accountant")
        self.assertEqual(res.analyst_input.request.role, "chief_accountant")
        flagged = [c for c in res.analyst_input.candidates if "unreadable_claims" in c.quality_flags]
        self.assertEqual(len(flagged), 1)  # спортивная новость с повреждённой кодировкой

    def test_fake_run_noise_is_excluded(self):
        """Конвейер целиком на FakeLLM: шум (Getty Images, спорт) → exclude, нет вызова для нечитаемого."""

        def responder(req: ChatRequest) -> str:
            content = req.messages[-1]["content"]
            if "Getty" in content or "Эрдоган" in content:
                return json.dumps(exclude_draft("Нет механизма: событие о зарубежной компании без связи с признаками"), ensure_ascii=False)
            return json.dumps(draft(decision="watch", materiality="low"), ensure_ascii=False)

        fake = FakeLLM(responder)
        analyst = BusinessAnalyst(fake, model="z-ai/glm-5.3-flash", prompt_version=V11, max_parallel=1)
        out, _ = analyst.analyze(self.ad.to_analyst_input(self.batches["U_100044"]).analyst_input)
        self.assertEqual(len(fake.requests), 5)
        reasons = {e.event_refs[0].event_id: e.reason_code for e in out.excluded_candidates}
        self.assertEqual(reasons.get("experiment:b1f952fd5e23147e7280b24e"), "not_relevant")  # Getty Images
        self.assertIn("unreadable_source", reasons.values())


@unittest.skipUnless(RESULTS.is_file(), "нет eval/handoff8_results.json")
class TestRecordedLiveRun(unittest.TestCase):
    """Регресс по записанному живому прогону: шум не получает include."""

    def test_noise_is_never_include(self):
        data = json.loads(RESULTS.read_text(encoding="utf-8"))
        noise = ("Getty Images", "Эрдоган", "Батракова", "ДТП", "Единой России", "Уиткофф", "Ушаков", "iPhone", "АЭС")
        rows = data["candidates"]
        self.assertEqual(len(rows), 48)
        for r in rows:
            if any(n in r["title"] for n in noise):
                self.assertIn(r["decision"], ("exclude", "watch"), r["title"])
                self.assertTrue(r["reason"], r["title"])
