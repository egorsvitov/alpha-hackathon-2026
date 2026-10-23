"""Тесты моста ``AnalystOutput`` → контракт фронта (``Insight``, ``FeedItem``) и правила приоритета.

Без сети: ответы модели подставляет ``FakeLLM``. Правило значимости (шкала 0..1):
high >= 0,50; medium 0,25-0,49; low < 0,25. ``exclude`` скрывается из ленты, ``watch`` не выше medium,
``impact_estimate`` показывается только при high. Формы сверяются со схемами ``app/schemas/base.py``.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from app.analyst.analyst import (
    EVIDENCE_ADD,
    MATERIALITY_BASE,
    URGENCY_ADD,
    level_from_score,
    priority_from,
)
from app.analyst.qa import QAResult
from app.schemas.base import Evidence, FeedItem, Insight, Relevance, TraceStep

from tests.analyst.helpers import (
    bundle_event,
    draft,
    exclude_draft,
    make_bundle,
    make_service,
    responder_by_content,
    seed_company,
    seed_event,
)

EVIDENCE_TYPES = {"news", "bank_research", "company_feature", "synthetic"}
ESTIMATE = {
    "metric": "Процентные расходы",
    "scenario": "Если ставка сохранится, расходы на обслуживание кредитов не снизятся",
    "assumptions": ["Ставка по кредитам плавающая"],
    "claim_ids": ["C1"],
    "feature_ids": ["F1"],
}


def score_fn(event, company, role):
    from app.services.store import store

    try:
        return store._score_event_for_company(event, company, role)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return Relevance(score=0.0, level="low", explanation="нет сигнала")


def template_fail():
    raise AssertionError("шаблонный откат не должен вызываться")


def dumps(d: dict[str, Any]):
    return lambda req: json.dumps(d, ensure_ascii=False)


def insight_for(d: dict[str, Any], event_id: str = "e1_key_rate", company_id: str = "SYNTHETIC_U_1042", role: str = "cfo") -> Insight:
    svc, _, _ = make_service(responder=dumps(d))
    return svc.insight(seed_company(company_id), role, seed_event(event_id), score_fn, template_fail)


def qa_result(decision="include", inference="supported", penalty=0.0) -> QAResult:
    return QAResult(decision=decision, inference_status=inference, event_status="occurred", headline="h", penalty=penalty)


class TestPriorityRule(unittest.TestCase):
    def test_level_thresholds_at_boundaries(self):
        cases = [(0.95, "high"), (0.50, "high"), (0.4999, "medium"), (0.49, "medium"), (0.25, "medium"), (0.2499, "low"),
                 (0.24, "low"), (0.0, "low")]
        for score, level in cases:
            self.assertEqual(level_from_score(score), level, score)

    def test_scale_constants(self):
        self.assertEqual(MATERIALITY_BASE, {"high": 0.62, "medium": 0.32, "low": 0.08})
        self.assertEqual(URGENCY_ADD, {"high": 0.10, "medium": 0.04, "low": 0.0})
        self.assertEqual(EVIDENCE_ADD["supported"], 0.06)

    def test_high_materiality_is_high(self):
        score, level, bd = priority_from(qa_result(), "high", "high")
        self.assertAlmostEqual(score, 0.78)
        self.assertEqual(level, "high")
        self.assertEqual((bd.materiality, bd.urgency, bd.evidence, bd.cap), (0.62, 0.10, 0.06, None))

    def test_medium_materiality_never_becomes_high(self):
        for urgency in ("high", "medium", "low"):
            for status in ("supported", "partially_supported"):
                score, level, _ = priority_from(qa_result(inference=status), "medium", urgency)
                self.assertLess(score, 0.50, (urgency, status))
                self.assertEqual(level, "medium")

    def test_low_materiality_is_low(self):
        score, level, _ = priority_from(qa_result(), "low", "high")
        self.assertAlmostEqual(score, 0.24)  # 0,08 + 0,10 + 0,06
        self.assertEqual(level, "low")

    def test_watch_is_capped_at_medium(self):
        score, level, bd = priority_from(qa_result(decision="watch"), "high", "high")
        self.assertEqual(score, 0.45)
        self.assertEqual(level, "medium")
        self.assertEqual(bd.cap, 0.45)
        self.assertIn("watch", bd.cap_reason)

    def test_hypothesis_is_capped(self):
        score, level, bd = priority_from(qa_result(inference="hypothesis"), "high", "high")
        self.assertEqual((score, level), (0.45, "medium"))
        self.assertIn("гипотеза", bd.cap_reason)

    def test_insufficient_evidence_is_low(self):
        score, level, bd = priority_from(qa_result(decision="watch", inference="insufficient_evidence"), "high", "high")
        self.assertEqual((score, level, bd.cap), (0.20, "low", 0.20))

    def test_qa_penalty_lowers_score_and_can_change_level(self):
        score, level, bd = priority_from(qa_result(penalty=0.2), "high", "low")  # 0,62 + 0,06 - 0,20
        self.assertAlmostEqual(score, 0.48)
        self.assertEqual(level, "medium")
        self.assertEqual(bd.penalty, 0.2)

    def test_score_never_negative_or_above_cap(self):
        self.assertEqual(priority_from(qa_result(inference="hypothesis", penalty=0.2), "low", "low")[0], 0.0)
        self.assertLessEqual(priority_from(qa_result(), "high", "high")[0], 0.95)

    def test_unknown_levels_do_not_crash(self):
        score, level, _ = priority_from(qa_result(), "weird", "weird")
        self.assertEqual(level, "low")

    def test_rule_reaches_frontend_levels(self):
        for materiality, expected in (("high", "high"), ("medium", "medium"), ("low", "low")):
            ins = insight_for(draft(materiality=materiality, urgency="medium"))
            self.assertEqual(ins.relevance.level, expected, materiality)
        # итоговый уровень фронта совпадает с порогами по баллу
        for materiality in ("high", "medium", "low"):
            rel = insight_for(draft(materiality=materiality)).relevance
            self.assertEqual(rel.level, level_from_score(rel.score))


class TestWatchAndExclude(unittest.TestCase):
    NO_LINK = dict(
        materiality="high", urgency="high",
        company_relevance={"text": "Событие может касаться компании.", "kind": "inference", "claim_ids": ["C1"]},
        impact_mechanism={"text": "Возможно влияние на издержки.", "kind": "inference", "claim_ids": ["C1"]},
    )

    def test_watch_decision_is_medium_or_low(self):
        ins = insight_for(draft(decision="watch", materiality="high", urgency="high"))
        self.assertIn(ins.relevance.level, ("medium", "low"))
        self.assertLessEqual(ins.relevance.score, 0.45)
        self.assertTrue(any("watch" in r for r in (ins.relevance.reasons or [])) or ins.relevance.score <= 0.45)

    def test_include_without_company_link_is_downgraded_to_watch(self):
        ins = insight_for(draft(**self.NO_LINK))
        self.assertNotEqual(ins.relevance.level, "high")
        self.assertLessEqual(ins.relevance.score, 0.45)
        self.assertTrue(any("не подтверждена" in u.text for u in ins.unknowns))

    def test_watch_with_high_materiality_mentions_cap_in_reasons(self):
        ins = insight_for(draft(decision="watch", materiality="high", urgency="high", inference_status="supported"))
        self.assertTrue(any("Значимость ограничена" in r for r in ins.relevance.reasons))

    def test_exclude_card_is_flat_low(self):
        ins = insight_for(exclude_draft("Нет связи с бизнесом"), event_id="e7_noise_sports")
        self.assertEqual((ins.relevance.level, ins.relevance.score), ("low", 0.0))
        self.assertEqual(ins.relevance.evidence_status, "excluded")
        self.assertEqual(ins.status, "ok")
        self.assertEqual(ins.risks, [])
        self.assertEqual(ins.opportunities, [])
        self.assertEqual(ins.next_actions, [])
        self.assertIsNone(ins.impact_estimate)
        self.assertIsNone(ins.bank_position)
        self.assertIsNone(ins.ai_conclusion)
        self.assertEqual(len(ins.why_it_matters), 1)
        self.assertIn("не нашёл механизма влияния", ins.why_it_matters[0].text)
        self.assertEqual(ins.why_it_matters[0].kind, "inference")
        self.assertGreaterEqual(len(ins.what_happened.facts), 1)

    def feed(self, hide: bool, events=None):
        responder = responder_by_content([("шахмат", exclude_draft())])
        svc, _, _ = make_service(responder=responder, extra_env={"ANALYST_FEED_HIDE_EXCLUDED": "true" if hide else "false"})
        events = events or [seed_event("e7_noise_sports"), seed_event("e1_key_rate"), seed_event("e3_rub_weak")]
        return svc.feed(seed_company("SYNTHETIC_U_3305"), "cfo", events, score_fn)

    def test_exclude_hidden_from_feed_by_default(self):
        ids = [f.event.event_id for f in self.feed(hide=True)]
        self.assertNotIn("e7_noise_sports", ids)
        self.assertEqual(sorted(ids), ["e1_key_rate", "e3_rub_weak"])

    def test_flag_false_keeps_excluded_event_as_low(self):
        items = self.feed(hide=False)
        by_id = {f.event.event_id: f for f in items}
        self.assertIn("e7_noise_sports", by_id)
        noise = by_id["e7_noise_sports"]
        self.assertEqual((noise.relevance.level, noise.relevance.score), ("low", 0.0))
        self.assertEqual(items[-1].event.event_id, "e7_noise_sports")  # внизу ленты
        self.assertTrue(noise.relevance.explanation.startswith("Не влияет на компанию"))

    def test_flag_default_is_hide(self):
        svc, _, _ = make_service()
        self.assertTrue(svc.settings.feed_hide_excluded)
        from app.analyst.settings import AnalystSettings

        self.assertTrue(AnalystSettings.from_env({}).feed_hide_excluded)
        self.assertFalse(AnalystSettings.from_env({"ANALYST_FEED_HIDE_EXCLUDED": "false"}).feed_hide_excluded)
        self.assertFalse(AnalystSettings.from_env({"ANALYST_FEED_HIDE_EXCLUDED": "0"}).feed_hide_excluded)
        self.assertTrue(AnalystSettings.from_env({"ANALYST_FEED_HIDE_EXCLUDED": "yes"}).feed_hide_excluded)

    def test_user_uploaded_news_is_never_hidden(self):
        uploaded = {"event_id": "user_1", "title": "Городской турнир по шахматам", "date": "2026-10-21", "topic": "other",
                    "topic_label": "Пользовательское", "sources_count": 1, "is_synthetic": True, "user_uploaded": True,
                    "text": "Финальный этап турнира по шахматам пройдёт в выходные."}
        responder = responder_by_content([("шахмат", exclude_draft())])
        svc, _, _ = make_service(responder=responder, extra_env={"ANALYST_FEED_HIDE_EXCLUDED": "true"})
        feed = svc.feed(seed_company("SYNTHETIC_U_3305"), "cfo", [uploaded, seed_event("e1_key_rate")], score_fn)
        self.assertIn("user_1", [f.event.event_id for f in feed])

    def test_feed_is_sorted_by_score_descending(self):
        items = self.feed(hide=False)
        scores = [f.relevance.score for f in items]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_duplicates_are_always_hidden(self):
        a = make_bundle("dup_a", "ЕС вводит квоты на прокат", [{"text": "ЕС вводит квоты на прокат."}], cluster_id="cl_q")
        b = make_bundle("dup_b", "Квоты ЕС на стальной прокат", [{"text": "Евросоюз ограничит импорт проката."}], cluster_id="cl_q")
        svc, _, _ = make_service(extra_env={"ANALYST_FEED_HIDE_EXCLUDED": "false"})
        feed = svc.feed(seed_company(), "ceo", [bundle_event(a, cluster_id="cl_q"), bundle_event(b, cluster_id="cl_q")], score_fn)
        self.assertEqual(len(feed), 1)


class TestImpactEstimateOnlyWhenHigh(unittest.TestCase):
    def test_high_materiality_keeps_estimate(self):
        ins = insight_for(draft(materiality="high", urgency="high", impact_estimate=ESTIMATE))
        self.assertEqual(ins.relevance.level, "high")
        self.assertIsNotNone(ins.impact_estimate)
        self.assertEqual(ins.impact_estimate.metric, "Процентные расходы")
        self.assertEqual(ins.impact_estimate.assumptions, ["Ставка по кредитам плавающая"])
        known = {e.evidence_id for e in ins.evidence}
        self.assertTrue(set(ins.impact_estimate.evidence_ids) <= known)
        self.assertTrue(ins.impact_estimate.summary.startswith("Сценарий") or "Если" in ins.impact_estimate.summary)

    def test_medium_and_low_hide_estimate(self):
        for materiality in ("medium", "low"):
            ins = insight_for(draft(materiality=materiality, urgency="high", impact_estimate=ESTIMATE))
            self.assertNotEqual(ins.relevance.level, "high")
            self.assertIsNone(ins.impact_estimate, materiality)

    def test_watch_never_has_estimate(self):
        ins = insight_for(draft(decision="watch", materiality="high", urgency="high", impact_estimate=ESTIMATE))
        self.assertIsNone(ins.impact_estimate)

    def test_estimate_without_assumptions_is_never_shown(self):
        ins = insight_for(draft(materiality="high", urgency="high", impact_estimate={**ESTIMATE, "assumptions": []}))
        self.assertIsNone(ins.impact_estimate)


class TestInsightShape(unittest.TestCase):
    def setUp(self):
        d = draft(
            materiality="high", urgency="high", impact_estimate=ESTIMATE,
            bank_assessment={"status": "found", "bank_chunk_ids": ["B1"]},
            next_steps=[
                {"text": "Проверить структуру долга.", "roles": ["cfo"], "feature_ids": ["F1"]},
                {"text": "Обсудить сценарии с руководством.", "roles": ["ceo"], "feature_ids": ["F1"]},
            ],
            risks=[{"text": "Возможен рост издержек.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]},
                   {"text": "Ставка остаётся высокой.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}],
        )
        self.ins = insight_for(d, role="cfo")

    def test_roundtrip_through_pydantic_schema(self):
        again = Insight.model_validate(json.loads(self.ins.model_dump_json()))
        self.assertEqual(again, self.ins)

    def test_top_level_keys_match_frontend_schema(self):
        self.assertEqual(set(self.ins.model_dump()), set(Insight.model_fields))

    def test_scalar_fields(self):
        i = self.ins
        self.assertEqual((i.company_id, i.user_role, i.status), ("SYNTHETIC_U_1042", "cfo", "ok"))
        self.assertEqual(i.profile_version, "p-2026-10-20-a1")
        self.assertEqual(i.generated_by, "live")
        self.assertTrue(i.run_id.startswith("an_"))
        self.assertEqual(i.event.event_id, "e1_key_rate")

    def test_facts_are_verbatim_source_claims_with_evidence(self):
        self.assertTrue(self.ins.what_happened.facts)
        for f in self.ins.what_happened.facts:
            self.assertEqual(f.kind, "fact")
            self.assertTrue(f.evidence_ids)
            self.assertFalse(f.conditional)
        self.assertEqual(self.ins.what_happened.summary, self.ins.what_happened.summary.strip())

    def test_every_referenced_evidence_and_feature_is_in_evidence_list(self):
        ids = {e.evidence_id for e in self.ins.evidence}
        claims = (self.ins.what_happened.facts + self.ins.why_it_matters + self.ins.risks + self.ins.opportunities + self.ins.next_actions)
        for c in claims:
            for e in c.evidence_ids:
                self.assertIn(e, ids)
            for f in c.features or []:
                self.assertIn(f"cf:{f}", ids)
        if self.ins.ai_conclusion:
            for e in self.ins.ai_conclusion.evidence_ids:
                self.assertIn(e, ids)

    def test_evidence_items_are_well_formed_and_unique(self):
        ids = [e.evidence_id for e in self.ins.evidence]
        self.assertEqual(len(ids), len(set(ids)))
        for e in self.ins.evidence:
            self.assertIsInstance(e, Evidence)
            self.assertIn(e.type, EVIDENCE_TYPES)
            self.assertTrue(e.quote and e.title and e.publisher)
            if e.type == "company_feature":
                self.assertTrue(e.evidence_id.startswith("cf:"))
                self.assertTrue(e.is_synthetic)

    def test_evidence_order_is_first_mention_order(self):
        order = []
        for c in self.ins.what_happened.facts + self.ins.why_it_matters:
            for e in c.evidence_ids:
                if e not in order:
                    order.append(e)
        listed = [e.evidence_id for e in self.ins.evidence if not e.evidence_id.startswith("cf:")]
        self.assertEqual(listed[: len(order)], order)

    def test_hypothesis_claims_are_conditional_and_inference_are_not(self):
        hyp = next(c for c in self.ins.risks if c.text == "Возможен рост издержек.")
        inf = next(c for c in self.ins.risks if c.text == "Ставка остаётся высокой.")
        self.assertEqual((hyp.kind, hyp.conditional), ("hypothesis", True))
        self.assertEqual((inf.kind, inf.conditional), ("inference", None))

    def test_next_steps_have_roles_and_selected_role_first(self):
        steps = self.ins.next_actions
        self.assertEqual(steps[0].text, "Проверить структуру долга.")
        self.assertEqual(steps[0].roles, ["cfo"])
        self.assertTrue(all(s.kind != "fact" for s in steps))
        ceo = insight_for(draft(next_steps=[
            {"text": "Проверить структуру долга.", "roles": ["cfo"], "feature_ids": ["F1"]},
            {"text": "Обсудить сценарии с руководством.", "roles": ["ceo"], "feature_ids": ["F1"]},
        ]), role="ceo")
        self.assertEqual(ceo.next_actions[0].text, "Обсудить сценарии с руководством.")

    def test_bank_position_is_verbatim_quote_and_separate_from_ai(self):
        bp = self.ins.bank_position
        self.assertIsNotNone(bp)
        self.assertEqual(bp.as_of, "2026-09-11")
        bank_ev = next(e for e in self.ins.evidence if e.type == "bank_research")
        self.assertEqual(bp.claims[0].text, bank_ev.quote)  # дословная цитата, а не пересказ модели
        self.assertEqual(bp.claims[0].kind, "fact")
        self.assertEqual(bp.claims[0].evidence_ids, [bank_ev.evidence_id])
        self.assertNotEqual(self.ins.ai_conclusion.text, bp.claims[0].text)
        self.assertIsNotNone(self.ins.ai_conclusion)

    def test_trace_is_numbered_and_shows_pipeline(self):
        self.assertEqual([t.n for t in self.ins.trace], list(range(1, len(self.ins.trace) + 1)))
        tools = [t.tool for t in self.ins.trace]
        for step in ("input_adapter", "bank_rag_search", "llm_analyst", "qa_validator"):
            self.assertIn(step, tools)
        for t in self.ins.trace:
            self.assertIsInstance(t, TraceStep)
            self.assertIn(t.status, ("ok", "retry", "warn"))

    def test_relevance_factors_sum_to_score(self):
        rel = self.ins.relevance
        self.assertAlmostEqual(sum(f.contribution for f in rel.factors), rel.score, places=3)
        self.assertEqual(len(rel.factors), 3)
        self.assertEqual({f.feature_key for f in rel.factors}, {"analyst.materiality", "analyst.urgency", "analyst.evidence"})
        self.assertTrue(rel.explanation)
        self.assertIn(rel.level, ("high", "medium", "low"))
        self.assertEqual(rel.evidence_status, "supported")
        self.assertEqual(rel.candidate_channels, ["costs"])

    def test_unknowns_include_bank_note_only_when_no_bank_position(self):
        self.assertFalse(any("Позиции аналитики Альфа-Банка" in u.text for u in self.ins.unknowns))
        no_bank = insight_for(draft(), event_id="e7_noise_sports")
        self.assertTrue(any("Позиции аналитики Альфа-Банка по этому событию нет" in u.text for u in no_bank.unknowns))
        self.assertLessEqual(len(no_bank.unknowns), 8)

    def test_outdated_bank_adds_warning(self):
        from app.schemas.base import Evidence as Ev
        from app.analyst.input_adapter import bank_fragments_from_evidence

        old = Ev(evidence_id="b_old", type="bank_research", title="Ключевая ставка: прогноз", publisher="ЦМИ Альфа-Банка", date="2026-06-01",
                 quote="Ожидаем снижения ключевой ставки летом.", is_synthetic=True)
        svc, _, _ = make_service(responder=dumps(draft(bank_assessment={"status": "found", "bank_chunk_ids": ["B1"]})))
        svc.bank_corpus = bank_fragments_from_evidence([old])
        ins = svc.insight(seed_company(), "cfo", seed_event("e1_key_rate"), score_fn, template_fail)
        self.assertTrue(any("неактуальной" in u.text for u in ins.unknowns))
        self.assertIsNotNone(ins.bank_position)

    def test_insufficient_evidence_gives_insufficient_data_status(self):
        ghost = {"kind": "inference", "feature_ids": ["F99"], "claim_ids": ["C99"]}
        ins = insight_for(draft(company_relevance={"text": "Влияет.", **ghost}, impact_mechanism={"text": "Через.", **ghost},
                                risks=[{"text": "Риск.", **ghost}], ai_conclusion={"text": "Вывод.", **ghost}))
        self.assertEqual(ins.status, "insufficient_data")
        self.assertLessEqual(ins.relevance.score, 0.20)
        self.assertEqual(ins.relevance.level, "low")

    def test_hypothesis_status_marks_reasons(self):
        ins = insight_for(draft(inference_status="hypothesis", materiality="high", urgency="high"))
        self.assertLessEqual(ins.relevance.score, 0.45)
        self.assertEqual(ins.relevance.evidence_status, "hypothesis")


class TestFeedItemShape(unittest.TestCase):
    def feed(self, d=None):
        svc, _, _ = make_service(responder=dumps(d or draft(bank_assessment={"status": "found", "bank_chunk_ids": ["B1"]})))
        events = [seed_event("e1_key_rate"), seed_event("e3_rub_weak"), seed_event("e2_eu_steel_quota")]
        return svc.feed(seed_company(), "cfo", events, score_fn)

    def test_items_validate_against_schema(self):
        items = self.feed()
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertIsInstance(item, FeedItem)
            self.assertEqual(FeedItem.model_validate(json.loads(item.model_dump_json())), item)
            self.assertEqual(set(item.model_dump()), set(FeedItem.model_fields))

    def test_item_fields(self):
        item = next(i for i in self.feed() if i.event.event_id == "e1_key_rate")
        self.assertEqual(item.profile_version, "p-2026-10-20-a1")
        self.assertEqual(item.why_short, item.relevance.explanation)
        self.assertTrue(item.has_bank_position)
        self.assertEqual(item.event.title, seed_event("e1_key_rate")["title"])
        self.assertEqual(item.relevance.level, level_from_score(item.relevance.score))

    def test_has_bank_position_false_without_bank_fragment(self):
        items = self.feed(draft())
        self.assertTrue(all(not i.has_bank_position for i in items))

    def test_same_card_and_feed_relevance(self):
        svc, _, _ = make_service(responder=dumps(draft(materiality="high", urgency="high")))
        company, event = seed_company(), seed_event("e1_key_rate")
        feed_item = svc.feed(company, "cfo", [event], score_fn)[0]
        card = svc.insight(company, "cfo", event, score_fn, template_fail)
        self.assertEqual(feed_item.relevance, card.relevance)  # лента и карточка не расходятся

    def test_one_llm_call_per_candidate_and_cache_reused(self):
        svc, fake, _ = make_service()
        events = [seed_event("e1_key_rate"), seed_event("e3_rub_weak")]
        svc.feed(seed_company(), "cfo", events, score_fn)
        self.assertEqual(len(fake.requests), 2)
        svc.feed(seed_company(), "cfo", events, score_fn)
        svc.insight(seed_company(), "cfo", events[0], score_fn, template_fail)
        self.assertEqual(len(fake.requests), 2)  # повторы из кэша: бюджет не расходуется

    def test_role_changes_cache_key(self):
        svc, fake, _ = make_service()
        event = seed_event("e1_key_rate")
        svc.insight(seed_company(), "cfo", event, score_fn, template_fail)
        svc.insight(seed_company(), "ceo", event, score_fn, template_fail)
        self.assertEqual(len(fake.requests), 2)

    def test_generated_by_cached_when_served_from_bundled_cache(self):
        from app.analyst.settings import PACKAGE_DIR

        svc, _, _ = make_service(mode="cached", with_llm=False, bundled=PACKAGE_DIR / "cache")
        ins = svc.insight(seed_company("SYNTHETIC_U_1042"), "cfo", seed_event("e1_key_rate"), score_fn, template_fail)
        self.assertEqual(ins.generated_by, "cached")
        self.assertTrue(any(t.tool == "cache_lookup" for t in ins.trace))
        Insight.model_validate(json.loads(ins.model_dump_json()))


if __name__ == "__main__":
    unittest.main()
