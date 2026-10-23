"""Тесты адаптера входа ``app.analyst.input_adapter``: наши данные → ``AnalystInput.v1-draft``.

Проверяется сборка из seed-компаний и событий, обработка неизвестного (``None`` не превращается в
«нет»/0), события без разметки (загрузка пользователя), поиск по банку с учётом ``as_of``,
детерминизм хэшей и отсутствие побочных эффектов на входных словарях.
"""

from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from app.analyst import input_adapter as ia
from app.analyst.schemas import ANALYST_INPUT_VERSION, ROLES, AnalystInput, BankFragment, MatchContext
from app.schemas.base import Relevance, RelevanceFactor
from app.services.seed_data import COMPANIES_DATA, EVENTS_DATA, SHARED_EVIDENCE

from tests.analyst.helpers import AS_OF, analyst_input, bundle_event, make_bundle, seed_company, seed_event

AS_OF_DT = datetime.fromisoformat(AS_OF)
MSK = timezone(timedelta(hours=3))


def feature(ctx, feature_id):
    return next((f for f in ctx.verified_facts if f.feature_id == feature_id), None)


class TestParseDt(unittest.TestCase):
    def test_date_only_is_moscow_midnight(self):
        dt = ia.parse_dt("2026-10-20")
        self.assertEqual(dt, datetime(2026, 10, 20, 0, 0, tzinfo=MSK))

    def test_naive_datetime_is_moscow(self):
        self.assertEqual(ia.parse_dt("2026-10-20T10:30:00").utcoffset(), timedelta(hours=3))

    def test_explicit_offset_and_z_are_kept(self):
        self.assertEqual(ia.parse_dt("2026-10-20T10:30:00+05:00").utcoffset(), timedelta(hours=5))
        self.assertEqual(ia.parse_dt("2026-10-20T10:30:00Z").utcoffset(), timedelta(0))

    def test_garbage_and_empty_give_none(self):
        for bad in (None, "", "вчера", "2026-13-45", "2026/10/20 99:99"):
            self.assertIsNone(ia.parse_dt(bad), bad)


class TestSeedCompanyContext(unittest.TestCase):
    def test_all_seed_companies_build_valid_context(self):
        for c in COMPANIES_DATA:
            ctx = ia.company_context(c)
            self.assertEqual(ctx.profile_version, c["profile_version"])
            self.assertTrue(ctx.synthetic_demo_profile)
            ids = [f.feature_id for f in ctx.verified_facts]
            self.assertEqual(len(ids), len(set(ids)), "feature_id должны быть уникальны")
            self.assertTrue(ctx.unknowns)  # стандартные «неизвестно» есть всегда

    def test_company_1042_fields(self):
        ctx = ia.company_context(seed_company("SYNTHETIC_U_1042"))
        self.assertIn("62.4", str(feature(ctx, "company.revenue").value))
        self.assertIn("прошлый отчётный год", feature(ctx, "company.revenue").description)
        self.assertEqual(feature(ctx, "product.loans").value, "есть")
        self.assertEqual(feature(ctx, "product.deposits").value, "нет")
        self.assertEqual(feature(ctx, "fts.flag").value, "есть")
        self.assertEqual(feature(ctx, "fts.country").value, "Казахстан")
        self.assertIn("Металлургия", feature(ctx, "company.industry").value)

    def test_unknown_product_is_not_turned_into_false(self):
        ctx = ia.company_context(seed_company("SYNTHETIC_U_2218"))  # leasing = None
        self.assertIsNone(feature(ctx, "product.leasing"))
        self.assertTrue(any("Лизинг" in u and "неизвестно" in u for u in ctx.unknowns))
        self.assertEqual(feature(ctx, "product.loans").value, "нет")  # явное False остаётся «нет»
        self.assertFalse(any(e.exposure_id == "exp.leasing" for e in ctx.economic_exposures))

    def test_no_fts_means_no_exposure_to_fx(self):
        ctx = ia.company_context(seed_company("SYNTHETIC_U_3305"))
        self.assertEqual(feature(ctx, "fts.flag").value, "нет")
        self.assertIsNone(feature(ctx, "fts.country"))
        self.assertFalse(any(e.exposure_id == "exp.fx_trade" for e in ctx.economic_exposures))

    def test_exposures_only_from_present_products(self):
        ctx = ia.company_context(seed_company("SYNTHETIC_U_1042"))
        ids = {e.exposure_id for e in ctx.economic_exposures}
        self.assertEqual(ids, {"exp.guarantees", "exp.loans", "exp.leasing", "exp.fx_trade"})
        loans = next(e for e in ctx.economic_exposures if e.exposure_id == "exp.loans")
        self.assertEqual(loans.status, "inferred")  # выведено, а не подтверждено
        self.assertEqual(loans.basis_feature_ids, ["product.loans"])
        self.assertIn("условия договоров неизвестны", loans.description)

    def test_counterparties_are_payment_links_not_suppliers(self):
        ctx = ia.company_context(seed_company("SYNTHETIC_U_1042"))
        cps = [f for f in ctx.verified_facts if f.feature_id.startswith("counterparty.")]
        self.assertEqual(len(cps), 4)
        for f in cps:
            self.assertIn("тип отношений по платежам не подтверждён", f.description)
        self.assertIn("исходящие платежи", cps[0].value)
        self.assertIn("21", cps[0].value)
        rel = ctx.related_entities[0]
        self.assertEqual((rel.relation, rel.share_pct), ("payment_out", 21.0))
        self.assertEqual(ctx.related_entities[2].relation, "payment_in")
        self.assertTrue(ctx.graph_context.included)

    def test_unknown_revenue_is_not_zero(self):
        company = {"company_id": "SYNTHETIC_U_X", "name": "X", "industry": "Торговля", "revenue_bn_rub": 0, "profile_version": "p1"}
        ctx = ia.company_context(company)
        self.assertIsNone(feature(ctx, "company.revenue"))
        self.assertTrue(any("Выручка компании неизвестна" in u for u in ctx.unknowns))

    def test_empty_company_does_not_crash_and_invents_nothing(self):
        ctx = ia.company_context({"company_id": "SYNTHETIC_U_EMPTY"})
        self.assertEqual(ctx.profile_version, "unknown")
        self.assertEqual(ctx.verified_facts, [])
        self.assertEqual(ctx.economic_exposures, [])
        self.assertFalse(ctx.graph_context.included)
        self.assertTrue(any("неизвестно" in u for u in ctx.unknowns))
        self.assertGreaterEqual(sum("продукта" in u for u in ctx.unknowns), 7)  # все 7 продуктов неизвестны

    def test_input_company_dict_is_not_mutated(self):
        company = seed_company("SYNTHETIC_U_1042")
        before = copy.deepcopy(company)
        ia.company_context(company)
        analyst_input(company, "cfo", [seed_event("e1_key_rate")])
        self.assertEqual(company, before)

    def test_profile_snapshot_ref_and_origin(self):
        ctx = ia.company_context(seed_company("SYNTHETIC_U_1042"))
        self.assertEqual(ctx.profile_snapshot_ref, "seed:SYNTHETIC_U_1042@p-2026-10-20-a1")
        self.assertTrue(all(f.origin.type == "seed_profile" for f in ctx.verified_facts))


class TestRankingProfileContext(unittest.TestCase):
    PROFILE = {
        "company_id": "DEMO_RANK_1", "profile_version": "rp-7", "is_synthetic": True,
        "missing_fields": ["revenue/period"], "limitations": ["Профиль собран из тестовых данных."],
        "features": {
            "DEMO_RANK_1:/products/loan": {"path": "/products/loan", "value": True, "quality": "observed", "origin": {"kind": "csv", "source_path": "a.csv"}},
            "DEMO_RANK_1:/products/leasing": {"path": "/products/leasing", "value": None, "quality": "unknown", "origin": {"kind": "csv"}},
            "DEMO_RANK_1:/fts_present": {"path": "/fts_present", "value": True, "quality": "observed", "origin": {"kind": "csv"}},
            "DEMO_RANK_1:/revenue/raw": {"path": "/revenue/raw", "value": 62400000000, "quality": "observed", "origin": {"kind": "csv"}},
            "DEMO_RANK_1:/revenue/unit": {"path": "/revenue/unit", "value": "rub", "quality": "observed", "origin": {"kind": "csv"}},
            "DEMO_RANK_1:/region": {"path": "/region", "value": "Тестовая область", "quality": "derived", "origin": {"kind": "derived"}},
        },
    }

    def test_features_unknowns_and_exposures(self):
        ctx = ia.company_context(self.PROFILE)
        self.assertEqual(ctx.profile_version, "rp-7")
        ids = {f.feature_id for f in ctx.verified_facts}
        self.assertIn("DEMO_RANK_1:/products/loan", ids)
        self.assertNotIn("DEMO_RANK_1:/products/leasing", ids)  # неизвестное — в unknowns, а не в факты
        self.assertNotIn("DEMO_RANK_1:/revenue/unit", ids)  # служебные поля пропускаются
        self.assertTrue(any("Лизинг: неизвестно" in u for u in ctx.unknowns))
        self.assertTrue(any("Нет данных: revenue/period" in u for u in ctx.unknowns))
        self.assertIn("Профиль собран из тестовых данных.", ctx.unknowns)
        self.assertEqual({e.exposure_id for e in ctx.economic_exposures}, {"exp.loans", "exp.fx_trade"})

    def test_revenue_marked_as_unconfirmed_unit(self):
        ctx = ia.company_context(self.PROFILE)
        rev = feature(ctx, "DEMO_RANK_1:/revenue/raw")
        self.assertIn("не считать рублями за период", rev.description)

    def test_bool_values_are_localised_and_quality_kept(self):
        ctx = ia.company_context(self.PROFILE)
        self.assertEqual(feature(ctx, "DEMO_RANK_1:/products/loan").value, "есть")
        self.assertEqual(feature(ctx, "DEMO_RANK_1:/region").quality, "derived")

    def test_dispatch_by_features_key(self):
        self.assertEqual(ia.company_context(self.PROFILE).profile_snapshot_ref, "ranking-profile:DEMO_RANK_1")
        self.assertTrue(ia.company_context(seed_company()).profile_snapshot_ref.startswith("seed:"))


class TestAnalystInputAssembly(unittest.TestCase):
    def test_request_fields(self):
        inp = analyst_input(seed_company("SYNTHETIC_U_1042"), "treasurer", [seed_event("e1_key_rate")])
        self.assertEqual(inp.schema_version, ANALYST_INPUT_VERSION)
        self.assertEqual(inp.request.company_id, "SYNTHETIC_U_1042")
        self.assertEqual(inp.request.role, "treasurer")
        self.assertEqual(inp.request.as_of, AS_OF)
        self.assertEqual(inp.request.news_window_hours, 1440)
        self.assertTrue(inp.request.request_id.startswith("req:"))
        self.assertTrue(inp.analysis_policy.do_not_infer_supplier_from_payment)
        self.assertTrue(inp.analysis_policy.require_supporting_claim_and_feature_ids)

    def test_unknown_role_is_rejected(self):
        with self.assertRaises(ValidationError):
            analyst_input(seed_company(), "janitor", [seed_event("e1_key_rate")])

    def test_all_roles_are_accepted(self):
        for role in ROLES:
            self.assertEqual(analyst_input(seed_company(), role, [seed_event("e1_key_rate")]).request.role, role)

    def test_request_id_is_deterministic_and_input_dependent(self):
        a = analyst_input(seed_company(), "cfo", [seed_event("e1_key_rate")])
        b = analyst_input(seed_company(), "cfo", [seed_event("e1_key_rate")])
        self.assertEqual(a.request.request_id, b.request.request_id)
        self.assertNotEqual(a.request.request_id, analyst_input(seed_company(), "ceo", [seed_event("e1_key_rate")]).request.request_id)
        self.assertNotEqual(a.request.request_id, analyst_input(seed_company(), "cfo", [seed_event("e2_eu_steel_quota")]).request.request_id)

    def test_every_seed_company_and_event_builds_and_roundtrips(self):
        for c in COMPANIES_DATA:
            inp = ia.build_analyst_input(
                c, "cfo", copy.deepcopy(EVENTS_DATA), as_of=AS_OF_DT, bank_corpus=ia.bank_fragments_from_evidence(SHARED_EVIDENCE)
            )
            self.assertEqual(len(inp.candidates), len(EVENTS_DATA))
            again = AnalystInput.model_validate(json.loads(inp.model_dump_json()))
            self.assertEqual(again.request.request_id, inp.request.request_id)
            for cand in inp.candidates:
                bundle = cand.analysis_projection.event_bundles[0]
                self.assertTrue(bundle.claims, cand.candidate_id)
                self.assertTrue(bundle.evidence)

    def test_candidate_identity(self):
        inp = analyst_input(seed_company(), "cfo", [seed_event("e1_key_rate")])
        cand = inp.candidates[0]
        self.assertEqual(cand.candidate_id, "cand:e1_key_rate:v1")
        self.assertEqual(cand.cluster_id, "cluster:e1_key_rate")
        ref = cand.event_bundle_refs[0]
        self.assertEqual((ref.event_id, ref.event_version), ("e1_key_rate", 1))
        self.assertEqual(len(ref.snapshot_sha256), 64)

    def test_snapshot_hash_stable_and_sensitive_to_content(self):
        sha1 = analyst_input(seed_company(), "cfo", [seed_event("e1_key_rate")]).candidates[0].event_bundle_refs[0].snapshot_sha256
        sha2 = analyst_input(seed_company("SYNTHETIC_U_2218"), "ceo", [seed_event("e1_key_rate")]).candidates[0].event_bundle_refs[0].snapshot_sha256
        self.assertEqual(sha1, sha2)  # от компании и роли не зависит
        changed = make_bundle("e1_key_rate", "Другой заголовок", [{"text": "Другой текст события."}])
        sha3 = analyst_input(seed_company(), "cfo", [bundle_event(changed)]).candidates[0].event_bundle_refs[0].snapshot_sha256
        self.assertNotEqual(sha1, sha3)

    def test_cluster_id_from_event_or_bundle(self):
        bundle = make_bundle("c1", "Заголовок", [{"text": "Текст события."}], cluster_id="cl_x")
        inp = analyst_input(seed_company(), "cfo", [bundle_event(bundle)])
        self.assertEqual(inp.candidates[0].cluster_id, "cl_x")
        inp2 = analyst_input(seed_company(), "cfo", [bundle_event(bundle, cluster_id="cl_override")])
        self.assertEqual(inp2.candidates[0].cluster_id, "cl_override")

    def test_projection_keeps_claims_verbatim(self):
        seeded = ia.seed_bundle("e1_key_rate")
        cand = analyst_input(seed_company(), "cfo", [seed_event("e1_key_rate")]).candidates[0]
        proj = cand.analysis_projection.event_bundles[0]
        self.assertEqual([c.text for c in proj.claims], [c["text"] for c in seeded["claims"]])
        self.assertEqual([c.claim_id for c in proj.claims], [c["claim_id"] for c in seeded["claims"]])
        self.assertEqual(proj.claims[0].measurements, seeded["claims"][0]["measurements"])
        self.assertEqual(proj.claims[0].evidence_ids, seeded["claims"][0]["evidence_ids"])

    def test_seed_bundle_returns_independent_copy(self):
        a = ia.seed_bundle("e1_key_rate")
        a["claims"][0]["text"] = "испорчено"
        self.assertNotEqual(ia.seed_bundle("e1_key_rate")["claims"][0]["text"], "испорчено")
        self.assertIsNone(ia.seed_bundle("нет_такого_события"))

    def test_bundle_precedence(self):
        inner = make_bundle("p1", "Из ключа bundle", [{"text": "Утверждение из вложенного бандла."}])
        self.assertIs(ia.bundle_for_event({"event_id": "e1_key_rate", "bundle": inner}), inner)
        self.assertIs(ia.bundle_for_event(inner), inner)  # сам EventBundle
        self.assertEqual(ia.bundle_for_event({"event_id": "e1_key_rate", "title": "x"})["event"]["event_id"], "e1_key_rate")  # seed
        self.assertEqual(ia.bundle_for_event({"event_id": "zzz", "title": "Новость"})["schema_version"], "user-upload")


class TestUnknownEvents(unittest.TestCase):
    def test_unknown_event_gets_unverified_user_bundle(self):
        event = {"event_id": "user_9", "title": "Минпромторг обсуждает новые меры поддержки экспортёров", "date": "2026-10-21",
                 "topic": "trade", "text": "Меры обсуждаются на этой неделе. Подробности пока не раскрываются публично.", "user_uploaded": True}
        inp = analyst_input(seed_company(), "cfo", [event])
        bundle = inp.candidates[0].analysis_projection.event_bundles[0]
        self.assertEqual(bundle.bundle_schema_version, "user-upload")
        self.assertEqual(bundle.event.quality, "partial")
        self.assertEqual(bundle.claims[0].text, event["title"])
        self.assertTrue(all(c.modality == "unknown" for c in bundle.claims))  # модальность не выдумываем
        self.assertTrue(any("не проверены" in lim for lim in bundle.limitations))
        self.assertEqual(bundle.sources[0].publisher, "Загружено пользователем")
        self.assertTrue(bundle.sources[0].is_synthetic)
        self.assertEqual([e.quote for e in bundle.evidence], [c.text for c in bundle.claims])  # цитата = утверждение

    def test_text_is_split_into_short_verbatim_sentences(self):
        event = {"event_id": "user_2", "title": "Заголовок новости", "date": "2026-10-21",
                 "text": "Да. Первое достаточно длинное предложение. Второе достаточно длинное предложение. Короткое."}
        bundle = ia.bundle_from_news_event(event)
        texts = [c["text"] for c in bundle["claims"]]
        self.assertEqual(texts, ["Заголовок новости", "Первое достаточно длинное предложение.", "Второе достаточно длинное предложение."])

    def test_claims_are_capped_and_title_not_duplicated(self):
        sentences = " ".join(f"Предложение номер {i} достаточно длинное для утверждения." for i in range(10))
        bundle = ia.bundle_from_news_event({"event_id": "u", "title": "Заголовок", "date": "2026-10-21"}, text=sentences)
        self.assertEqual(len(bundle["claims"]), 5)  # заголовок + не больше 4 предложений
        same = ia.bundle_from_news_event({"event_id": "u", "title": "Это достаточно длинный заголовок новости."}, text="Это достаточно длинный заголовок новости.")
        self.assertEqual(len(same["claims"]), 1)

    def test_empty_text_gives_title_only(self):
        bundle = ia.bundle_from_news_event({"event_id": "u3", "title": "Только заголовок", "date": "2026-10-21"})
        self.assertEqual(len(bundle["claims"]), 1)
        self.assertEqual(bundle["claims"][0]["claim_id"], "u3:c1")
        self.assertEqual(bundle["claims"][0]["evidence_ids"], ["u3:ev1"])

    def test_company_without_id_does_not_break_assembly(self):
        inp = ia.build_analyst_input({"industry": "Торговля"}, "cfo", [seed_event("e1_key_rate")], as_of=AS_OF_DT)
        self.assertEqual(inp.request.company_id, "None")  # строка, а не исключение: ID проверяет вызывающий код
        self.assertEqual(len(inp.candidates), 1)

    def test_empty_event_list(self):
        inp = analyst_input(seed_company(), "cfo", [])
        self.assertEqual(inp.candidates, [])


class TestBankSearch(unittest.TestCase):
    def corpus(self) -> list[BankFragment]:
        return ia.bank_fragments_from_evidence(SHARED_EVIDENCE)

    def test_corpus_has_only_bank_research(self):
        frags = self.corpus()
        self.assertEqual({f.bank_chunk_id for f in frags}, {"b_key", "b_commodities"})
        self.assertTrue(all(f.quote for f in frags))
        self.assertEqual(ia.bank_fragments_from_evidence([]), [])

    def test_key_rate_event_finds_key_rate_fragment(self):
        cand = analyst_input(seed_company(), "cfo", [seed_event("e1_key_rate")]).candidates[0]
        self.assertEqual([f.bank_chunk_id for f in cand.bank_fragments], ["b_key"])
        self.assertEqual(cand.bank_fragments[0].applicability, "current")

    def test_unrelated_event_has_no_bank_fragments(self):
        cand = analyst_input(seed_company(), "cfo", [seed_event("e7_noise_sports")]).candidates[0]
        self.assertEqual(cand.bank_fragments, [])

    def test_fragment_published_after_as_of_is_not_used(self):
        bundle = ia.seed_bundle("e1_key_rate")
        early = datetime(2026, 9, 1, tzinfo=MSK)
        self.assertEqual(ia.search_bank_fragments(bundle, self.corpus(), early), [])
        self.assertTrue(ia.search_bank_fragments(bundle, self.corpus(), AS_OF_DT))

    def test_old_fragment_marked_outdated(self):
        bundle = ia.seed_bundle("e1_key_rate")
        bundle["event"]["first_published_at"] = "2026-12-20T10:00:00+03:00"
        found = ia.search_bank_fragments(bundle, self.corpus(), datetime(2027, 1, 1, tzinfo=MSK))
        self.assertEqual(found[0].bank_chunk_id, "b_key")
        self.assertEqual(found[0].applicability, "outdated")

    def test_corpus_objects_are_not_mutated(self):
        corpus = self.corpus()
        ia.search_bank_fragments(ia.seed_bundle("e1_key_rate"), corpus, AS_OF_DT)
        self.assertTrue(all(f.applicability == "unknown" for f in corpus))

    def test_top_k_and_overlap_threshold(self):
        bundle = ia.seed_bundle("e1_key_rate")
        self.assertLessEqual(len(ia.search_bank_fragments(bundle, self.corpus(), AS_OF_DT, top_k=1)), 1)
        self.assertEqual(ia.search_bank_fragments(bundle, self.corpus(), AS_OF_DT, min_overlap=500), [])


class TestMatchContext(unittest.TestCase):
    def test_no_relevance_gives_empty_context(self):
        mc = ia.match_context_from_relevance(None, ia.company_context(seed_company()), ia.seed_bundle("e1_key_rate"))
        self.assertEqual(mc, MatchContext())

    def test_template_factors_map_to_company_features(self):
        company = seed_company("SYNTHETIC_U_1042")
        rel = Relevance(score=0.6, level="high", explanation="Связано", reasons=["В профиле есть кредитный продукт"], factors=[
            RelevanceFactor(feature_key="product.loans", label="Кредитные продукты", contribution=0.35, detail="стоимость капитала"),
            RelevanceFactor(feature_key="fts", label="ВЭД", contribution=0.2, detail="внешняя торговля"),
            RelevanceFactor(feature_key="product.factoring", label="Факторинг", contribution=0.1, detail="нет у компании"),
        ])
        inp = analyst_input(company, "cfo", [seed_event("e1_key_rate")], relevance={"e1_key_rate": rel})
        mc = inp.candidates[0].match_context
        self.assertEqual(mc.matched_company_feature_ids, ["product.loans", "fts.flag", "fts.country", "product.factoring"])
        self.assertEqual(mc.prior_score, 0.6)
        self.assertEqual(len(mc.signals), 3)
        self.assertIn("В профиле есть кредитный продукт", mc.impact_hypotheses)
        self.assertTrue(mc.assumptions_not_verified)  # отбор — подсказка, а не доказательство

    def test_unknown_feature_key_maps_to_nothing(self):
        rel = {"score": 0.3, "factors": [{"feature_key": "weird.key", "label": "?", "detail": "-"}], "reasons": []}
        mc = ia.match_context_from_relevance(rel, ia.company_context(seed_company()), ia.seed_bundle("e1_key_rate"))
        self.assertEqual(mc.matched_company_feature_ids, [])

    def test_counterparty_name_in_claims_confirms_match(self):
        company = seed_company("SYNTHETIC_U_1042")
        ctx = ia.company_context(company)
        rel = {"score": 0.8, "factors": [], "reasons": []}
        self.assertTrue(ia.match_context_from_relevance(rel, ctx, ia.seed_bundle("e5_coke_supplier")).confirmed_entity_match)
        self.assertIsNone(ia.match_context_from_relevance(rel, ctx, ia.seed_bundle("e1_key_rate")).confirmed_entity_match)

    def test_matched_claim_ids_listed(self):
        bundle = ia.seed_bundle("e3_rub_weak")
        mc = ia.match_context_from_relevance({"score": 0.1, "factors": []}, ia.company_context(seed_company()), bundle)
        self.assertEqual(mc.matched_event_claim_ids, [c["claim_id"] for c in bundle["claims"]])

    def test_stable_hash_is_order_independent_for_dict_keys(self):
        self.assertEqual(ia.stable_hash({"a": 1, "b": [1, 2]}), ia.stable_hash({"b": [1, 2], "a": 1}))
        self.assertNotEqual(ia.stable_hash({"a": 1}), ia.stable_hash({"a": 2}))


if __name__ == "__main__":
    unittest.main()
