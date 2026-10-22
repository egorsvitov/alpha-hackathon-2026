"""Десять сценариев из ТЗ «LLM Business Analyst» v1.1, §6 (без сети, на FakeLLM).

Модель в тестах намеренно «ошибается» так, как это делают реальные LLM, и проверяется,
что детерминированный QA и мост в контракт фронта не пропускают ошибку к пользователю.
"""

from __future__ import annotations

import unittest

from app.schemas.base import Evidence, Relevance
from app.services.store import store

from tests.analyst.helpers import (
    bundle_event,
    draft,
    exclude_draft,
    feature_alias,
    load_fixture_bundle,
    make_bundle,
    make_service,
    responder_by_content,
    seed_company,
    seed_event,
)


def score_fn(event, company, role):
    try:
        return store._score_event_for_company(event, company, role)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return Relevance(score=0.0, level="low", explanation="нет сигнала")


def template_fail():
    raise AssertionError("не должен вызываться шаблонный откат")


def all_texts(insight) -> list[str]:
    out = [c.text for c in insight.what_happened.facts] + [insight.what_happened.summary]
    for group in (insight.why_it_matters, insight.risks, insight.opportunities, insight.next_actions):
        out += [c.text for c in group]
    if insight.ai_conclusion:
        out.append(insight.ai_conclusion.text)
    return out


class TestTzScenarios(unittest.TestCase):
    # (1) Ставка + кредит: влияние на новое кредитование, а не автоматический рост платежей по действующему договору.
    def test_01_rate_affects_new_lending_not_existing_contract(self):
        company = seed_company("SYNTHETIC_U_1042")
        loans = feature_alias(company, "product.loans")
        d = draft(
            materiality="high",
            risks=[
                {"text": "Платежи по действующему кредиту компании вырастут.", "kind": "inference", "feature_ids": [loans], "claim_ids": ["C1"]},
                {"text": "Новое финансирование останется дорогим.", "kind": "inference", "feature_ids": [loans], "claim_ids": ["C1"]},
            ],
            company_relevance={"text": "У компании есть кредитные продукты.", "kind": "inference", "feature_ids": [loans], "claim_ids": ["C1"]},
        )
        svc, fake, _ = make_service(responder=lambda req: __import__("json").dumps(d, ensure_ascii=False))
        ins = svc.insight(company, "cfo", seed_event("e1_key_rate"), score_fn, template_fail)
        existing = [r for r in ins.risks if "действующ" in r.text]
        self.assertEqual(len(existing), 1)
        self.assertEqual(existing[0].kind, "hypothesis")
        self.assertTrue(existing[0].conditional)
        self.assertTrue(any("ставк" in u.text.lower() and "договор" in u.text.lower() for u in ins.unknowns))
        self.assertTrue(any(t.tool == "qa_validator" and "contract_terms" in t.result for t in ins.trace))
        new = [r for r in ins.risks if "Новое финансирование" in r.text]
        self.assertEqual(new[0].kind, "inference")

    # (2) «Домодедово»: нельзя утверждать закрытие аэропорта/отмену рейсов, которых нет в источнике.
    def test_02_no_airport_closure_claims(self):
        bundle = make_bundle(
            "dme", "Аэропорт Домодедово сообщил о порядке обслуживания рейсов",
            [{"text": "Аэропорт обслуживает рейсы по согласованию с уполномоченными органами."}],
        )
        d = draft(
            headline="Аэропорт закрыт: рейсы отменены",
            what_happened=[{"text": "Аэропорт закрыт, все рейсы отменены.", "kind": "fact", "claim_ids": ["C1"]}],
            risks=[{"text": "Аэропорт закрыт, компания понесла убытки.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]},
                   {"text": "Возможны задержки перевозок, если компания использует аэропорт.", "kind": "hypothesis", "feature_ids": ["F1"], "claim_ids": ["C1"]}],
        )
        svc, _, _ = make_service(responder=lambda req: __import__("json").dumps(d, ensure_ascii=False))
        ins = svc.insight(seed_company(), "ceo", bundle_event(bundle), score_fn, template_fail)
        for text in all_texts(ins):
            self.assertNotIn("закрыт", text.lower(), text)
            self.assertNotIn("отмен", text.lower(), text)
        self.assertEqual(ins.what_happened.facts[0].text, "Аэропорт обслуживает рейсы по согласованию с уполномоченными органами.")
        self.assertEqual(ins.what_happened.summary, "Аэропорт Домодедово сообщил о порядке обслуживания рейсов")
        self.assertEqual(len(ins.risks), 1)

    # (3) Прогноз/сценарий не становится фактом; отрицание не меняет знак.
    def test_03_forecast_not_fact_and_negation_kept(self):
        bundle = load_fixture_bundle("rate_forecast.json")
        d = draft(event_status="occurred",
                  what_happened=[{"text": "Регулятор сохранит ключевую ставку 14% 23 октября 2026 года.", "kind": "fact", "claim_ids": ["C1"]}])
        svc, _, _ = make_service(responder=lambda req: __import__("json").dumps(d, ensure_ascii=False))
        company = seed_company()
        inp_out = svc.analyst.analyze_candidate(svc.build_input(company, "cfo", [bundle_event(bundle)], None),
                                                svc.build_input(company, "cfo", [bundle_event(bundle)], None).candidates[0])
        self.assertEqual(inp_out.insight.event_status, "forecast")
        self.assertTrue(inp_out.insight.facts[0].text.startswith("Прогноз источника"))

        neg = make_bundle("neg", "Регулятор опроверг слухи о повышении ставки",
                          [{"text": "Регулятор не принимал решения о повышении ключевой ставки.", "polarity": "negated"}])
        d2 = draft(event_status="occurred",
                   what_happened=[{"text": "Регулятор повысил ключевую ставку.", "kind": "fact", "claim_ids": ["C1"]}])
        svc2, _, _ = make_service(responder=lambda req: __import__("json").dumps(d2, ensure_ascii=False))
        inp = svc2.build_input(company, "cfo", [bundle_event(neg)], None)
        out = svc2.analyst.analyze_candidate(inp, inp.candidates[0])
        self.assertEqual(out.insight.event_status, "refuted")
        self.assertTrue(out.insight.facts[0].text.startswith("Не подтверждено"))

    # (4) Противоречащие источники не исчезают.
    def test_04_conflicting_sources_preserved(self):
        bundle = load_fixture_bundle("rate_conflict.json")
        d = draft(what_happened=[{"text": "Регулятор повысил ключевую ставку до 20%.", "kind": "fact", "claim_ids": ["C1"]}])
        svc, _, _ = make_service(responder=lambda req: __import__("json").dumps(d, ensure_ascii=False))
        ins = svc.insight(seed_company(), "cfo", bundle_event(bundle), score_fn, template_fail)
        quotes = " ".join(e.quote for e in ins.evidence)
        self.assertIn("20%", quotes)
        self.assertIn("21%", quotes)
        self.assertGreaterEqual(len(ins.what_happened.facts), 2)
        self.assertTrue(any("расходятся" in u.text for u in ins.unknowns))

    # (5) Неизвестные характеристики компании → ограниченная уверенность, без сильного вывода.
    def test_05_unknown_company_limits_confidence(self):
        company = {
            "company_id": "SYNTHETIC_U_NEW", "name": "Компания U_NEW", "industry": "", "revenue_bn_rub": 0, "age_label": "",
            "region": "", "products": {k: None for k in ("rko", "guarantees", "loans", "leasing", "deposits", "payroll", "factoring")},
            "fts": {"flag": None, "country": None}, "profile_version": "p-test",
        }
        d = draft(materiality="high", urgency="high",
                  company_relevance={"text": "Событие критично для компании.", "kind": "inference", "claim_ids": ["C1"]},
                  impact_mechanism={"text": "Издержки вырастут.", "kind": "inference", "claim_ids": ["C1"]},
                  impact_estimate={"metric": "Издержки", "scenario": "Если издержки вырастут, маржа снизится", "assumptions": ["Рост издержек"], "claim_ids": ["C1"]})
        svc, _, _ = make_service(responder=lambda req: __import__("json").dumps(d, ensure_ascii=False))
        ins = svc.insight(company, "ceo", seed_event("e6_food_inflation"), score_fn, template_fail)
        self.assertNotEqual(ins.relevance.level, "high")
        self.assertLessEqual(ins.relevance.score, 0.45)
        self.assertIsNone(ins.impact_estimate)
        self.assertTrue(any("неизвест" in u.text.lower() for u in ins.unknowns) or ins.relevance.missing_context)

    # (6) Платёжная связь не делает получателя поставщиком.
    def test_06_payment_does_not_make_supplier(self):
        company = seed_company("SYNTHETIC_U_1042")
        cp = feature_alias(company, "counterparty.1")
        d = draft(
            what_happened=[{"text": "«КоксРесурс» — ваш поставщик, он приостановил отгрузки.", "kind": "fact", "claim_ids": ["C1"]},
                           {"text": "«КоксРесурс» приостановил отгрузки угля.", "kind": "fact", "claim_ids": ["C1"]}],
            company_relevance={"text": "«КоксРесурс» — ключевой поставщик компании.", "kind": "inference", "feature_ids": [cp], "claim_ids": ["C1"]},
        )
        svc, _, _ = make_service(responder=lambda req: __import__("json").dumps(d, ensure_ascii=False))
        ins = svc.insight(company, "ceo", seed_event("e5_coke_supplier"), score_fn, template_fail)
        self.assertFalse(any("ваш поставщик" in f.text for f in ins.what_happened.facts))
        rel = next(c for c in ins.why_it_matters if "поставщик" in c.text)
        self.assertEqual(rel.kind, "hypothesis")
        self.assertTrue(rel.conditional)
        self.assertTrue(any("Тип отношений" in u.text for u in ins.unknowns))

    # (7) Bank RAG пуст/устарел → позицию банка не выдумывать.
    def test_07_no_invented_bank_position(self):
        d = draft(bank_assessment={"status": "found", "bank_chunk_ids": ["B1"]})
        svc, _, _ = make_service(responder=lambda req: __import__("json").dumps(d, ensure_ascii=False))
        company = seed_company()
        ins = svc.insight(company, "treasurer", seed_event("e3_rub_weak"), score_fn, template_fail)
        self.assertIsNone(ins.bank_position)
        self.assertFalse(any(e.type == "bank_research" for e in ins.evidence))
        feed = svc.feed(company, "treasurer", [seed_event("e3_rub_weak")], score_fn)
        self.assertFalse(feed[0].has_bank_position)

        old_bank = Evidence(evidence_id="b_old", type="bank_research", title="Ключевая ставка: прогноз", publisher="ЦМИ Альфа-Банка",
                            date="2026-06-01", quote="Ожидаем снижения ключевой ставки летом.", is_synthetic=True)
        svc2, _, _ = make_service(responder=lambda req: __import__("json").dumps(d, ensure_ascii=False))
        svc2.bank_corpus = __import__("app.analyst.input_adapter", fromlist=["x"]).bank_fragments_from_evidence([old_bank])
        ins2 = svc2.insight(company, "cfo", seed_event("e1_key_rate"), score_fn, template_fail)
        self.assertIsNotNone(ins2.bank_position)
        self.assertEqual(ins2.bank_position.claims[0].text, "Ожидаем снижения ключевой ставки летом.")
        self.assertTrue(any("неактуал" in u.text for u in ins2.unknowns))

    # (8) Нерелевантная новость исключается.
    def test_08_irrelevant_news_excluded(self):
        svc, _, _ = make_service(responder=responder_by_content([("шахмат", exclude_draft())]))
        company = seed_company("SYNTHETIC_U_3305")
        events = [seed_event("e7_noise_sports"), seed_event("e6_food_inflation")]
        feed = svc.feed(company, "cfo", events, score_fn)
        ids = [f.event.event_id for f in feed]
        self.assertNotIn("e7_noise_sports", ids)
        self.assertIn("e6_food_inflation", ids)
        ins = svc.insight(company, "cfo", seed_event("e7_noise_sports"), score_fn, template_fail)
        self.assertEqual(ins.relevance.level, "low")
        self.assertEqual(ins.relevance.score, 0.0)

    # (9) Два утверждения одной статьи не дают двух уведомлений; дубликаты кластера схлопываются.
    def test_09_no_duplicate_notifications(self):
        article = make_bundle("art", "Металлурги сообщили о росте цен на уголь",
                              [{"text": "Цены на коксующийся уголь выросли."}, {"text": "Металлурги ожидают дальнейшего роста цен.", "modality": "forecast"}])
        dup_a = make_bundle("dup_a", "ЕС вводит квоты на прокат", [{"text": "ЕС вводит квоты на прокат."}], cluster_id="cl_quota")
        dup_b = make_bundle("dup_b", "Квоты ЕС на стальной прокат", [{"text": "Евросоюз ограничит импорт проката квотами."}], cluster_id="cl_quota")
        svc, fake, _ = make_service()
        events = [bundle_event(article), bundle_event(dup_a, cluster_id="cl_quota"), bundle_event(dup_b, cluster_id="cl_quota")]
        feed = svc.feed(seed_company(), "ceo", events, score_fn)
        ids = [f.event.event_id for f in feed]
        self.assertEqual(ids.count("art"), 1)
        self.assertEqual(len([i for i in ids if i.startswith("dup_")]), 1)
        self.assertEqual(len(fake.requests), 3)  # один вызов на кандидата, а не на утверждение

    # (10) Новый допустимый company_id обрабатывается без ручной подготовки.
    def test_10_new_company_id_without_manual_prep(self):
        profile = {
            "company_id": "DEMO_NEW_777",
            "is_synthetic": True,
            "profile_version": "rp-1",
            "features": {
                "DEMO_NEW_777:/okved/code": {"feature_id": "DEMO_NEW_777:/okved/code", "path": "/okved/code", "value": "10.71", "quality": "observed", "origin": {"kind": "csv"}},
                "DEMO_NEW_777:/products/loan": {"feature_id": "DEMO_NEW_777:/products/loan", "path": "/products/loan", "value": True, "quality": "observed", "origin": {"kind": "csv"}},
                "DEMO_NEW_777:/products/payroll": {"feature_id": "DEMO_NEW_777:/products/payroll", "path": "/products/payroll", "value": None, "quality": "unknown", "origin": {"kind": "csv"}},
            },
        }
        svc, _, _ = make_service(responder=lambda req: __import__("json").dumps(draft(), ensure_ascii=False))
        rel = lambda e, c, r: Relevance(score=0.3, level="medium", explanation="тест")  # noqa: E731
        ins = svc.insight(profile, "cfo", seed_event("e1_key_rate"), rel, template_fail)
        self.assertEqual(ins.company_id, "DEMO_NEW_777")
        self.assertEqual(ins.profile_version, "rp-1")
        self.assertTrue(any(e.evidence_id.startswith("cf:DEMO_NEW_777:") for e in ins.evidence))
        self.assertEqual(ins.status, "ok")


if __name__ == "__main__":
    unittest.main()
