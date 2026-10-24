"""Тесты детерминированного валидатора ответа модели (``app.analyst.qa``) — без сети и LLM.

Модель в этих тестах намеренно «ошибается»: выдумывает ссылки и числа, выдаёт ущерб за факт,
«закрывает аэропорт», делает поставщика из платежа. Проверяется, что ошибка отброшена или
ослаблена, а ``qa_status`` и трасса честно её отражают.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from app.analyst import qa as qa_mod
from app.analyst.qa import QAResult, clean_text, numbers_in, validate_draft
from app.analyst.schemas import LLMDraft
from app.analyst.view import build_prompt_view

from tests.analyst.helpers import (
    AS_OF,
    analyst_input,
    bundle_event,
    draft,
    exclude_draft,
    feature_alias,
    make_bundle,
    make_service,
    seed_company,
    seed_event,
)

DEFAULT_CLAIMS = [{"text": "Центральный банк сообщил о решении по ключевой ставке на уровне 14% годовых."}]


def run_qa(d: dict[str, Any], claims: list[dict[str, Any]] | None = None, *, company: dict[str, Any] | None = None, role: str = "cfo",
           title: str = "Решение по ключевой ставке", conflicts: list[dict[str, Any]] | None = None):
    """Собирает кандидата из мини-бандла и прогоняет черновик через валидатор."""
    company = company or seed_company("SYNTHETIC_U_1042")
    bundle = make_bundle("t1", title, claims or DEFAULT_CLAIMS, conflicts=conflicts)
    inp = analyst_input(company, role, [bundle_event(bundle)])
    cand = inp.candidates[0]
    view = build_prompt_view(inp, cand)
    result = validate_draft(LLMDraft.model_validate(d), view, role, as_of=AS_OF)
    return result, view


def codes(result: QAResult, severity: str | None = None) -> list[str]:
    return [i.code for i in result.issues if severity is None or i.severity == severity]


class TestReferences(unittest.TestCase):
    def test_correct_draft_passes_without_issues(self):
        result, _ = run_qa(draft())
        self.assertEqual(result.issues, [])
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.penalty, 0.0)
        self.assertEqual(result.decision, "include")
        self.assertEqual(result.inference_status, "supported")
        self.assertEqual(result.statements_total, result.statements_with_refs)
        self.assertGreater(result.statements_total, 0)

    def test_aliases_are_translated_to_real_ids(self):
        result, view = run_qa(draft())
        real_claim = view.claims["C1"].claim_id
        real_feature = view.features["F1"].feature_id
        self.assertEqual(result.facts[0].claim_ids, [real_claim])
        self.assertEqual(result.facts[0].evidence_ids, view.claims["C1"].evidence_ids)
        self.assertEqual(result.company_relevance.feature_ids, [real_feature])
        self.assertEqual(result.company_relevance.claim_ids, [real_claim])

    def test_real_ids_are_accepted_too(self):
        probe, view = run_qa(draft())
        real_claim = view.claims["C1"].claim_id
        real_feature = view.features["F1"].feature_id
        d = draft(
            what_happened=[{"text": "Источник сообщает о событии.", "kind": "fact", "claim_ids": [real_claim]}],
            company_relevance={"text": "Событие касается отрасли.", "kind": "inference", "feature_ids": [real_feature], "claim_ids": [real_claim]},
        )
        result, _ = run_qa(d)
        self.assertNotIn("unknown_ref", codes(result))
        self.assertEqual(result.company_relevance.feature_ids, [real_feature])

    def test_nonexistent_claim_id_is_dropped_valid_ref_stays(self):
        d = draft(risks=[{"text": "Возможен рост издержек.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1", "C99"]}])
        result, view = run_qa(d)
        self.assertIn("unknown_ref", codes(result))
        self.assertEqual(result.risks[0].claim_ids, [view.claims["C1"].claim_id])
        self.assertNotIn("C99", json.dumps(result.risks[0].model_dump()))
        self.assertEqual(result.status, "passed_with_fixes")

    def test_nonexistent_feature_id_is_dropped_valid_ref_stays(self):
        d = draft(company_relevance={"text": "Событие касается отрасли.", "kind": "inference", "feature_ids": ["F1", "F77"], "claim_ids": ["C1"]})
        result, view = run_qa(d)
        self.assertEqual(result.company_relevance.feature_ids, [view.features["F1"].feature_id])
        issue = next(i for i in result.issues if i.code == "unknown_ref")
        self.assertIn("F77", issue.message)
        self.assertEqual(issue.path, "company_relevance")

    def test_made_up_evidence_style_ids_are_rejected(self):
        d = draft(risks=[{"text": "Возможен рост издержек.", "kind": "hypothesis", "feature_ids": ["F1"],
                          "claim_ids": ["ev-ghost-1", "t1:ev1", "uuid-0000"]}])
        result, view = run_qa(d)
        self.assertEqual(codes(result).count("unknown_ref"), 3)
        # evidence_id берётся только из существующих утверждений, а не из ссылок модели
        self.assertEqual(result.risks[0].evidence_ids, [])
        self.assertEqual(result.risks[0].claim_ids, [])
        self.assertEqual(result.risks[0].feature_ids, [view.features["F1"].feature_id])

    def test_extra_evidence_ids_field_from_model_is_ignored(self):
        d = draft()
        d["risks"][0]["evidence_ids"] = ["ghost-evidence"]
        result, view = run_qa(d)
        for st in result.all_statements():
            self.assertNotIn("ghost-evidence", st.evidence_ids)
            for ev in st.evidence_ids:
                self.assertIn(ev, view.evidence)

    def test_inference_with_only_unknown_refs_is_removed(self):
        d = draft(risks=[
            {"text": "Возможен рост издержек.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]},
            {"text": "Возможна потеря покупателей.", "kind": "hypothesis", "feature_ids": ["F88"], "claim_ids": ["C88"]},
        ])
        result, _ = run_qa(d)
        self.assertEqual(len(result.risks), 1)
        self.assertEqual(result.risks[0].text, "Возможен рост издержек.")
        self.assertIn("unsupported_statement", codes(result, "error"))

    def test_fact_without_valid_claim_is_removed_and_restored_verbatim(self):
        d = draft(what_happened=[{"text": "Выдуманное событие произошло.", "kind": "fact", "claim_ids": ["C42"]}])
        result, view = run_qa(d)
        self.assertIn("fact_without_source", codes(result, "error"))
        self.assertIn("facts_restored", codes(result))
        self.assertEqual(len(result.facts), 1)
        self.assertEqual(result.facts[0].text, view.claims["C1"].text)  # дословно из источника
        self.assertEqual(result.facts[0].kind, "fact")

    def test_fact_may_not_cite_only_company_feature(self):
        d = draft(what_happened=[{"text": "У компании есть кредиты.", "kind": "fact", "feature_ids": ["F1"]}])
        result, _ = run_qa(d)
        self.assertIn("fact_without_source", codes(result))

    def test_inference_with_no_references_at_all_is_removed(self):
        d = draft(risks=[{"text": "Возможен рост издержек.", "kind": "fact"}])
        result, _ = run_qa(d)
        self.assertEqual(result.risks, [])
        self.assertIn("unsupported_statement", codes(result, "error"))

    def test_all_supporting_ids_collected_are_real(self):
        result, view = run_qa(draft())
        feats, claims, evs, banks = result.support_ids()
        self.assertTrue(set(feats) <= {f.feature_id for f in view.features.values()})
        self.assertTrue(set(claims) <= {c.claim_id for c in view.claims.values()})
        self.assertTrue(set(evs) <= set(view.evidence))
        self.assertEqual(banks, [])

    def test_all_refs_unknown_means_no_analysis_and_failed(self):
        ghost = {"kind": "inference", "feature_ids": ["F99"], "claim_ids": ["C99"]}
        d = draft(
            company_relevance={"text": "Влияет на компанию.", **ghost}, impact_mechanism={"text": "Через издержки.", **ghost},
            risks=[{"text": "Риск.", **ghost}], ai_conclusion={"text": "Вывод.", **ghost},
        )
        result, _ = run_qa(d)
        self.assertIn("no_supported_analysis", codes(result, "error"))
        self.assertEqual(result.inference_status, "insufficient_evidence")
        self.assertEqual(result.decision, "watch")  # include без оснований понижается
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.risks, [])

    def test_unknown_bank_reference_is_rejected(self):
        d = draft(bank_assessment={"status": "found", "bank_chunk_ids": ["B9"]})
        result, view = run_qa(d, [{"text": "Клуб провёл товарищеский матч."}], title="Товарищеский матч")
        self.assertFalse(view.bank)  # у такого события нет фрагментов банка
        self.assertIn("bank_invented", codes(result, "error"))
        self.assertEqual(result.bank_assessment.status, "not_found")
        self.assertEqual(result.bank_assessment.bank_chunk_ids, [])

    def test_unknown_bank_chunk_with_existing_bank_fragments(self):
        company = seed_company("SYNTHETIC_U_1042")
        inp = analyst_input(company, "treasurer", [seed_event("e1_key_rate")])
        cand = inp.candidates[0]
        view = build_prompt_view(inp, cand)
        self.assertTrue(view.bank, "в seed для ключевой ставки должен найтись фрагмент банка")
        d = draft(bank_assessment={"status": "found", "bank_chunk_ids": ["B1", "B77"]})
        result = validate_draft(LLMDraft.model_validate(d), view, "treasurer", as_of=AS_OF)
        self.assertIn("unknown_bank_ref", codes(result, "error"))
        self.assertEqual(result.bank_assessment.bank_chunk_ids, [view.bank["B1"].bank_chunk_id])
        d2 = draft(bank_assessment={"status": "found", "bank_chunk_ids": []})
        result2 = validate_draft(LLMDraft.model_validate(d2), view, "treasurer", as_of=AS_OF)
        self.assertIn("bank_without_ref", codes(result2))
        self.assertEqual(result2.bank_assessment.status, "not_found")


class TestNumbers(unittest.TestCase):
    def test_numbers_in_ignores_aliases_and_handles_decimals(self):
        self.assertEqual(numbers_in("Ставка 14,5% и 20 [C1] F12 B3"), {14.5, 20.0})
        self.assertEqual(numbers_in(""), set())
        self.assertEqual(numbers_in("нет цифр"), set())

    def test_clean_text_strips_alias_brackets(self):
        self.assertEqual(clean_text("Рост издержек [C1, F2] возможен ."), "Рост издержек возможен.")
        self.assertEqual(clean_text("Факт (C1)."), "Факт.")

    def test_number_not_in_data_drops_statement(self):
        d = draft(risks=[
            {"text": "Издержки вырастут на 83%.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]},
            {"text": "Возможен рост издержек.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]},
        ])
        result, _ = run_qa(d)
        self.assertEqual([r.text for r in result.risks], ["Возможен рост издержек."])
        issue = next(i for i in result.issues if i.code == "number_not_in_data")
        self.assertEqual(issue.severity, "error")
        self.assertIn("83", issue.message)
        self.assertEqual(result.penalty, 0.05)

    def test_number_from_claim_is_allowed(self):
        d = draft(risks=[{"text": "При ставке 14% кредиты остаются дорогими.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}])
        result, _ = run_qa(d)
        self.assertEqual(len(result.risks), 1)
        self.assertNotIn("number_not_in_data", codes(result))

    def test_number_from_company_feature_is_allowed(self):
        d = draft(company_relevance={"text": "Выручка 62,4 млрд ₽ делает компанию заметной.", "kind": "inference", "feature_ids": ["F3"], "claim_ids": ["C1"]})
        result, _ = run_qa(d)
        self.assertNotIn("number_not_in_data", codes(result))
        self.assertIsNotNone(result.company_relevance)

    def test_number_from_measurement_is_allowed(self):
        claims = [{"text": "Квота на импорт проката ограничена.", "measurements": [{"metric": "квота", "value": 30, "unit": "тыс. т"}]}]
        d = draft(risks=[{"text": "Квота 30 тыс. тонн ограничивает импорт.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}])
        result, _ = run_qa(d, claims)
        self.assertNotIn("number_not_in_data", codes(result))

    def test_invented_number_in_headline_replaced_by_event_title(self):
        result, _ = run_qa(draft(headline="Ставка вырастет до 25%"), title="Решение по ключевой ставке")
        self.assertEqual(result.headline, "Решение по ключевой ставке")
        self.assertIn("headline_rewritten", codes(result))

    def test_invented_number_in_unknowns_and_checks_dropped(self):
        d = draft(unknowns=[{"text": "Доля кредитов 73% неизвестна."}, {"text": "Тип ставки по кредитам неизвестен."}],
                  recommended_checks=["Проверить 55 договоров", "Проверить тип ставки"])
        result, _ = run_qa(d)
        texts = [u.text for u in result.unknowns]
        self.assertIn("Тип ставки по кредитам неизвестен.", texts)
        self.assertFalse(any("73" in t for t in texts))
        self.assertEqual(result.recommended_checks, ["Проверить тип ставки"])

    def test_invented_number_in_fact_drops_it(self):
        d = draft(what_happened=[{"text": "Ставка составила 99%.", "kind": "fact", "claim_ids": ["C1"]}])
        result, view = run_qa(d)
        self.assertIn("number_not_in_data", codes(result))
        self.assertEqual(result.facts[0].text, view.claims["C1"].text)  # восстановлен дословный факт

    def test_revenue_recalculated_to_period_is_dropped(self):
        d = draft(company_relevance={"text": "Выручка за квартал составит 62,4 млрд ₽.", "kind": "inference", "feature_ids": ["F3"], "claim_ids": ["C1"]})
        result, _ = run_qa(d)
        self.assertIn("revenue_period", codes(result, "error"))
        self.assertIsNone(result.company_relevance)


class TestForbiddenPatterns(unittest.TestCase):
    AIRPORT = [{"text": "Аэропорт обслуживает рейсы по согласованию с уполномоченными органами."}]

    def test_unsupported_airport_closure_fact_is_dropped(self):
        d = draft(
            headline="Аэропорт закрыт",
            what_happened=[{"text": "Аэропорт закрыт, рейсы отменены.", "kind": "fact", "claim_ids": ["C1"]}],
        )
        result, view = run_qa(d, self.AIRPORT, title="Аэропорт сообщил о порядке обслуживания")
        self.assertIn("unsupported_strong_claim", codes(result, "error"))
        self.assertEqual(result.headline, "Аэропорт сообщил о порядке обслуживания")
        self.assertEqual(result.facts[0].text, view.claims["C1"].text)
        self.assertNotIn("закрыт", result.facts[0].text)

    def test_hedged_closure_inference_is_weakened_to_hypothesis(self):
        d = draft(risks=[{"text": "Возможно, аэропорт будет закрыт, и поставки задержатся.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}])
        result, _ = run_qa(d, self.AIRPORT)
        self.assertEqual(result.risks[0].kind, "hypothesis")
        self.assertTrue(result.risks[0].conditional)
        self.assertIn("unsupported_strong_claim", codes(result))

    def test_unhedged_closure_inference_is_dropped(self):
        d = draft(risks=[{"text": "Аэропорт закрыт, поставки сорваны.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}])
        result, _ = run_qa(d, self.AIRPORT)
        self.assertEqual(result.risks, [])

    def test_closure_allowed_when_source_says_so(self):
        claims = [{"text": "Аэропорт закрыт для приёма рейсов до 18:00 из-за ограничений."}]
        d = draft(what_happened=[{"text": "Аэропорт закрыт для приёма рейсов до 18:00.", "kind": "fact", "claim_ids": ["C1"]}])
        result, _ = run_qa(d, claims, title="Аэропорт закрыт для рейсов")
        self.assertNotIn("unsupported_strong_claim", codes(result))
        self.assertEqual(result.facts[0].text, "Аэропорт закрыт для приёма рейсов до 18:00.")

    def test_strong_words_allowed_in_next_steps(self):
        d = draft(next_steps=[{"text": "Проверить, нет ли запрета на поставки.", "roles": ["cfo"], "feature_ids": ["F1"]}])
        result, _ = run_qa(d)
        self.assertNotIn("unsupported_strong_claim", codes(result))
        self.assertEqual(len(result.next_steps), 1)

    def test_damage_as_fact_inference_weakened(self):
        d = draft(risks=[{"text": "Компания понесла убытки из-за решения регулятора.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}])
        result, _ = run_qa(d)
        issue = next(i for i in result.issues if i.code == "damage_as_fact")
        self.assertEqual(issue.severity, "error")
        self.assertEqual(result.risks[0].kind, "hypothesis")
        self.assertTrue(result.risks[0].conditional)

    def test_damage_as_fact_in_facts_dropped(self):
        d = draft(what_happened=[{"text": "Выручка упала.", "kind": "fact", "claim_ids": ["C1"]}])
        result, view = run_qa(d)
        self.assertIn("damage_as_fact", codes(result))
        self.assertEqual(result.facts[0].text, view.claims["C1"].text)

    def test_hedged_damage_is_not_flagged(self):
        d = draft(risks=[{"text": "Возможно, компания понесла бы убытки, если ставка вырастет.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]}])
        result, _ = run_qa(d)
        self.assertNotIn("damage_as_fact", codes(result))
        self.assertEqual(len(result.risks), 1)

    def test_loss_amount_stated_as_done_is_flagged(self):
        d = draft(risks=[{"text": "Ущерб составил значительную сумму.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}])
        result, _ = run_qa(d)
        self.assertIn("damage_as_fact", codes(result))

    def test_payment_does_not_make_supplier_fact(self):
        company = seed_company("SYNTHETIC_U_1042")
        cp = feature_alias(company, "counterparty.1")
        claims = [{"text": "Компания «КоксРесурс» приостановила отгрузки угля."}]
        d = draft(
            what_happened=[{"text": "«КоксРесурс» — ваш поставщик, он приостановил отгрузки.", "kind": "fact", "claim_ids": ["C1"]}],
            company_relevance={"text": "«КоксРесурс» — ключевой поставщик компании.", "kind": "inference", "feature_ids": [cp], "claim_ids": ["C1"]},
        )
        result, view = run_qa(d, claims, company=company, title="КоксРесурс приостановил отгрузки")
        self.assertIn("payment_as_supplier", codes(result))
        self.assertEqual(result.facts[0].text, view.claims["C1"].text)  # из факта формулировка убрана
        self.assertEqual(result.company_relevance.kind, "hypothesis")
        self.assertTrue(result.company_relevance.conditional)
        self.assertTrue(any("Тип отношений" in lim for lim in result.limitations))

    def test_hedged_hypothesis_about_supplier_is_accepted(self):
        company = seed_company("SYNTHETIC_U_1042")
        cp = feature_alias(company, "counterparty.1")
        d = draft(company_relevance={"text": "Возможно, «КоксРесурс» является вашим поставщиком; это нужно проверить.", "kind": "hypothesis", "feature_ids": [cp], "claim_ids": ["C1"]})
        result, _ = run_qa(d, company=company)
        self.assertNotIn("payment_as_supplier", codes(result))
        self.assertEqual(result.limitations, [])

    def test_contract_terms_without_data_become_conditional_with_unknown(self):
        d = draft(risks=[{"text": "Платежи по действующему кредиту вырастут.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}])
        result, _ = run_qa(d)
        self.assertIn("contract_terms", codes(result))
        self.assertEqual(result.risks[0].kind, "hypothesis")
        self.assertTrue(result.risks[0].conditional)
        self.assertTrue(any("ставки" in u.text and "договор" in u.text for u in result.unknowns))

    def test_identification_of_depersonalised_company_is_dropped(self):
        d = draft(company_relevance={"text": "Компания — ПАО «Северсталь» и зависит от экспорта.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]})
        result, _ = run_qa(d)
        self.assertIn("identification", codes(result, "error"))
        self.assertIsNone(result.company_relevance)

    def test_non_russian_answer_is_flagged(self):
        d = draft(
            headline="Key rate decision",
            company_relevance={"text": "The event may affect the company costs significantly.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]},
            impact_mechanism={"text": "Costs may grow through financing channels.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]},
            risks=[{"text": "Possible increase of costs.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]}],
            ai_conclusion={"text": "Check the exposure.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]},
        )
        result, _ = run_qa(d)
        self.assertIn("not_russian", codes(result, "error"))


class TestModalityAndConflicts(unittest.TestCase):
    def test_forecast_claim_gets_mark_and_status(self):
        claims = [{"text": "Регулятор сохранит ключевую ставку 14% 23 октября.", "modality": "forecast"}]
        d = draft(event_status="occurred", what_happened=[{"text": "Регулятор сохранит ключевую ставку 14%.", "kind": "fact", "claim_ids": ["C1"]}])
        # «сохранит» не содержит маркера прогноза в словаре, поэтому пометка добавляется
        result, _ = run_qa(d, claims)
        self.assertEqual(result.event_status, "forecast")
        self.assertIn("event_status_fixed", codes(result))
        self.assertIn("forecast_as_fact", codes(result))
        self.assertTrue(result.facts[0].text.startswith("Прогноз источника: "))

    def test_negated_claim_gets_mark(self):
        claims = [{"text": "Регулятор принимал решения о повышении ставки.", "polarity": "negated"}]
        d = draft(event_status="occurred", what_happened=[{"text": "Регулятор повысил ставку.", "kind": "fact", "claim_ids": ["C1"]}])
        result, _ = run_qa(d, claims)
        self.assertEqual(result.event_status, "refuted")
        self.assertTrue(result.facts[0].text.startswith("Не подтверждено / опровергнуто: "))

    def test_conflict_variants_are_restored_into_facts(self):
        claims = [{"text": "Регулятор повысил ставку до 20%."}, {"text": "Регулятор повысил ставку до 21%."}]
        conflicts = [{"description": "Источники называют разные значения ставки",
                      "variants": [{"claim_id": "t1:c1"}, {"claim_id": "t1:c2"}]}]
        d = draft(what_happened=[{"text": "Регулятор повысил ставку до 20%.", "kind": "fact", "claim_ids": ["C1"]}])
        result, _ = run_qa(d, claims, conflicts=conflicts)
        self.assertEqual(len(result.facts), 2)
        self.assertIn("conflict_variant_restored", codes(result))
        self.assertTrue(any("расходятся" in u.text for u in result.unknowns))
        self.assertTrue(any("расходятся" in lim for lim in result.limitations))
        self.assertEqual(result.event_status, "mixed")


class TestDecisionConsistency(unittest.TestCase):
    def test_include_without_company_feature_link_becomes_watch(self):
        d = draft(
            company_relevance={"text": "Событие может касаться компании.", "kind": "inference", "claim_ids": ["C1"]},
            impact_mechanism={"text": "Возможно влияние на издержки.", "kind": "inference", "claim_ids": ["C1"]},
        )
        result, _ = run_qa(d)
        self.assertEqual(result.decision, "watch")
        self.assertIn("no_company_link", codes(result))
        self.assertEqual(result.inference_status, "partially_supported")
        self.assertTrue(any("не подтверждена признаками" in u.text for u in result.unknowns))

    def test_exclude_keeps_decision_and_facts(self):
        result, view = run_qa(exclude_draft())
        self.assertEqual(result.decision, "exclude")
        self.assertEqual(result.facts[0].claim_ids, [view.claims["C1"].claim_id])
        self.assertIsNone(result.company_relevance)

    def test_hypothesis_relevance_lowers_supported_status(self):
        d = draft(company_relevance={"text": "Возможно, событие касается компании.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]})
        result, _ = run_qa(d)
        self.assertEqual(result.inference_status, "partially_supported")

    def test_insufficient_evidence_never_stays_include(self):
        result, _ = run_qa(draft(inference_status="insufficient_evidence"))
        self.assertEqual(result.decision, "watch")

    def test_statement_limits_and_dedup(self):
        same = {"text": "Возможен рост издержек.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]}
        many = [{"text": f"Риск номер {chr(1072 + i)}.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]} for i in range(6)]
        result, _ = run_qa(draft(risks=[same, dict(same)] + many))
        self.assertLessEqual(len(result.risks), 3)
        self.assertIn("duplicate_statement", codes(result, "info"))
        self.assertEqual(sum(1 for r in result.risks if r.text == same["text"]), 1)

    def test_next_steps_sorted_for_role(self):
        steps = [
            {"text": "Шаг для директора.", "roles": ["ceo"], "feature_ids": ["F1"]},
            {"text": "Шаг для всех.", "roles": [], "feature_ids": ["F1"]},
            {"text": "Шаг для финансового директора.", "roles": ["cfo"], "feature_ids": ["F1"]},
        ]
        result, _ = run_qa(draft(next_steps=steps), role="cfo")
        self.assertEqual([s.text for s in result.next_steps], ["Шаг для финансового директора.", "Шаг для всех.", "Шаг для директора."])
        self.assertTrue(all(s.kind != "fact" for s in result.next_steps))


class TestImpactEstimateAndBank(unittest.TestCase):
    EST = {"metric": "Процентные расходы", "scenario": "Если ставка останется 14%, расходы на обслуживание сохранятся",
           "assumptions": ["Ставка по кредитам плавающая"], "claim_ids": ["C1"], "feature_ids": ["F1"]}

    def test_valid_estimate_is_kept_with_assumptions(self):
        result, _ = run_qa(draft(impact_estimate=self.EST))
        self.assertIsNotNone(result.impact_estimate)
        self.assertEqual(result.impact_estimate.assumptions, ["Ставка по кредитам плавающая"])

    def test_estimate_without_assumptions_is_removed(self):
        result, _ = run_qa(draft(impact_estimate={**self.EST, "assumptions": []}))
        self.assertIsNone(result.impact_estimate)
        self.assertIn("estimate_without_assumptions", codes(result))

    def test_estimate_with_invented_numbers_is_removed(self):
        est = {**self.EST, "scenario": "Расходы вырастут на 340 млн ₽"}
        result, _ = run_qa(draft(impact_estimate=est))
        self.assertIsNone(result.impact_estimate)
        self.assertIn("estimate_number_not_in_data", codes(result, "error"))

    def test_estimate_numbers_from_assumptions_are_allowed(self):
        est = {**self.EST, "scenario": "Расходы вырастут примерно на 5 млн ₽", "assumptions": ["Допущение: рост долга на 5 млн ₽"]}
        result, _ = run_qa(draft(impact_estimate=est))
        self.assertIsNotNone(result.impact_estimate)

    def test_estimate_without_references_is_removed(self):
        result, _ = run_qa(draft(impact_estimate={**self.EST, "claim_ids": ["C55"], "feature_ids": ["F55"]}))
        self.assertIsNone(result.impact_estimate)
        self.assertIn("estimate_without_refs", codes(result))

    def test_estimate_scenario_gets_marker_when_not_hedged(self):
        est = {**self.EST, "scenario": "Расходы на обслуживание кредитов вырастут пропорционально ставке"}
        result, _ = run_qa(draft(impact_estimate=est))
        self.assertTrue(result.impact_estimate.scenario.startswith("Сценарий: "))

    def test_outdated_bank_fragment_marks_status(self):
        company = seed_company("SYNTHETIC_U_1042")
        inp = analyst_input(company, "cfo", [seed_event("e1_key_rate")])
        cand = inp.candidates[0]
        cand.bank_fragments[0].applicability = "outdated"
        view = build_prompt_view(inp, cand)
        d = draft(bank_assessment={"status": "found", "bank_chunk_ids": ["B1"]})
        result = validate_draft(LLMDraft.model_validate(d), view, "cfo", as_of=AS_OF)
        self.assertEqual(result.bank_assessment.status, "outdated")
        self.assertIn("bank_outdated", codes(result))
        self.assertTrue(any("неактуал" in lim for lim in result.limitations))


class TestStatusAndTrace(unittest.TestCase):
    def test_status_passed_fixes_failed(self):
        passed, _ = run_qa(draft())
        self.assertEqual(passed.status, "passed")
        fixes, _ = run_qa(draft(risks=[{"text": "Возможен рост.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1", "C9"]}]))
        self.assertEqual(fixes.status, "passed_with_fixes")
        ghost = {"kind": "inference", "feature_ids": ["F99"], "claim_ids": ["C99"]}
        failed, _ = run_qa(draft(company_relevance={"text": "Влияет.", **ghost}, impact_mechanism={"text": "Через.", **ghost},
                                 risks=[{"text": "Риск.", **ghost}], ai_conclusion={"text": "Вывод.", **ghost}))
        self.assertEqual(failed.status, "failed")

    def test_penalty_is_five_percent_per_error_capped(self):
        many = [{"text": f"Издержки вырастут на {70 + i}%.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]} for i in range(6)]
        result, _ = run_qa(draft(risks=many))
        self.assertEqual(len(codes(result, "error")), 6)
        self.assertEqual(result.penalty, 0.2)  # потолок штрафа

    def test_info_issues_do_not_change_status_or_penalty(self):
        same = {"text": "Возможен рост издержек.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]}
        result, _ = run_qa(draft(risks=[same, dict(same)]))
        self.assertEqual(codes(result), ["duplicate_statement"])
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.penalty, 0.0)

    def test_issue_messages_are_short_and_have_paths(self):
        result, _ = run_qa(draft(risks=[{"text": "Рост на 83%.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}]))
        for issue in result.issues:
            self.assertLessEqual(len(issue.message), 300)
            self.assertTrue(issue.path)
        self.assertEqual(result.issues[0].path, "risks[0]")

    def test_trace_and_run_status_in_analyst_pipeline(self):
        d = draft(risks=[
            {"text": "Издержки вырастут на 83%.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]},
            {"text": "Возможен рост издержек.", "kind": "hypothesis", "feature_ids": ["F1", "F66"], "claim_ids": ["C1"]},
        ])
        svc, fake, _ = make_service(responder=lambda req: json.dumps(d, ensure_ascii=False))
        company = seed_company()
        inp = svc.build_input(company, "cfo", [seed_event("e1_key_rate")], None)
        outcome = svc.analyst.analyze_candidate(inp, inp.candidates[0])
        self.assertEqual(outcome.run.qa_status, "passed_with_fixes")
        issue_codes = {i.code for i in outcome.run.qa_issues}
        self.assertTrue({"number_not_in_data", "unknown_ref"} <= issue_codes)
        step = next(t for t in outcome.run.trace if t.step == "qa_validator")
        self.assertIn("passed_with_fixes", step.result)
        self.assertIn("number_not_in_data", step.result)
        self.assertEqual(step.status, "warn")
        self.assertGreater(outcome.insight.priority_breakdown.penalty, 0)  # штраф QA попал в приоритет
        ids = [t.step for t in outcome.run.trace]
        self.assertLess(ids.index("llm_analyst"), ids.index("qa_validator"))
        if "priority_rule" in ids:  # порядок шагов, если правило приоритета попало в трассу
            self.assertLess(ids.index("qa_validator"), ids.index("priority_rule"))

    def test_clean_run_has_ok_qa_trace(self):
        svc, _, _ = make_service(responder=lambda req: json.dumps(draft(), ensure_ascii=False))
        inp = svc.build_input(seed_company(), "cfo", [seed_event("e1_key_rate")], None)
        outcome = svc.analyst.analyze_candidate(inp, inp.candidates[0])
        step = next(t for t in outcome.run.trace if t.step == "qa_validator")
        self.assertEqual(outcome.run.qa_status, "passed")
        self.assertEqual(step.status, "ok")
        self.assertEqual(outcome.insight.priority_breakdown.penalty, 0.0)

    def test_qa_recomputed_when_reading_from_cache(self):
        d = draft(risks=[{"text": "Издержки вырастут на 83%.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}])
        svc, fake, _ = make_service(responder=lambda req: json.dumps(d, ensure_ascii=False))
        inp = svc.build_input(seed_company(), "cfo", [seed_event("e1_key_rate")], None)
        first = svc.analyst.analyze_candidate(inp, inp.candidates[0])
        second = svc.analyst.analyze_candidate(inp, inp.candidates[0])
        self.assertEqual(len(fake.requests), 1)  # повтор — из кэша
        self.assertEqual(second.run.qa_status, first.run.qa_status)
        self.assertTrue(any(t.step == "cache_lookup" for t in second.run.trace))
        self.assertEqual([r.text for r in second.insight.risks], [r.text for r in first.insight.risks])

    def test_module_exposes_pattern_constants(self):
        self.assertTrue(qa_mod.STRONG_EVENT_STEMS)
        self.assertTrue(qa_mod.DAMAGE_PATTERNS)


if __name__ == "__main__":
    unittest.main()
