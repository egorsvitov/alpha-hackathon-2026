"""Режимы ``ANALYST_MODE``: template (по умолчанию), llm и cached — без сети и без расхода бюджета.

* ``template`` — прежнее шаблонное поведение, аналитик и LLM не используются;
* ``cached`` — только сохранённые ответы из репозитория, сеть запрещена (проверяется перехватом);
* ``llm`` — кэш → живой вызов (здесь ``FakeLLM``) → сохранённый ответ → шаблон; без ключа не падает.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from app.analyst import service as service_mod
from app.analyst import settings as settings_mod
from app.analyst.llm_gateway import BudgetLedger, ChatRequest, FakeLLM, LLMUnavailable
from app.analyst.service import AnalystService
from app.analyst.settings import PACKAGE_DIR, AnalystSettings
from app.schemas.base import Insight, QaAnswer, Relevance
from app.services.orchestrator import orchestrator
from app.services.store import store

from tests.analyst.helpers import AS_OF, draft, make_service, seed_company, seed_event

BUNDLED = PACKAGE_DIR / "cache"
TEMPLATE_TOOLS = ["profile_fetcher", "relevance_scorer", "bank_rag_search", "llm_analyst_synthesis", "qa_verifier"]
CO = "SYNTHETIC_U_1042"


def score_fn(event, company, role):
    try:
        return store._score_event_for_company(event, company, role)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return Relevance(score=0.0, level="low", explanation="нет сигнала")


def template_insight_fn(event_id="e1_key_rate", company_id=CO, role="cfo"):
    return lambda: orchestrator.run_pipeline(event=seed_event(event_id), company=seed_company(company_id), role=role, score_fn=score_fn)


def forbid_network():
    """Любая попытка сетевого обращения (httpx или сокет) — ошибка теста."""

    def boom(*args, **kwargs):
        raise AssertionError("сетевой вызов запрещён в этом режиме")

    return [
        mock.patch.object(httpx.Client, "send", boom),
        mock.patch.object(httpx.AsyncClient, "send", boom),
        mock.patch.object(socket.socket, "connect", boom),
    ]


class NetworkForbidden:
    def __enter__(self):
        self.patches = forbid_network()
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


class UseService(unittest.TestCase):
    """Подмена глобального сервиса аналитика с восстановлением (store/orchestrator берут его оттуда)."""

    def setUp(self):
        self._prev = service_mod._service
        self._events = list(store.user_events)
        self.addCleanup(self._restore)

    def _restore(self):
        service_mod.set_analyst_service(self._prev)
        store.user_events[:] = self._events

    def use(self, svc: AnalystService) -> AnalystService:
        service_mod.set_analyst_service(svc)
        return svc


class TestSettings(unittest.TestCase):
    def test_default_mode_is_template(self):
        s = AnalystSettings.from_env({})
        self.assertEqual(s.mode, "template")
        self.assertFalse(s.has_key)
        self.assertEqual(s.api_key, "")

    def test_default_mode_from_os_environ_without_dotenv(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(settings_mod, "_dotenv_loaded", True):
            s = AnalystSettings.from_env()
        self.assertEqual(s.mode, "template")
        self.assertFalse(s.has_key)

    def test_unknown_or_blank_mode_falls_back_to_template(self):
        for raw in ("", "   ", "turbo", "LLM2", "cache"):
            self.assertEqual(AnalystSettings.from_env({"ANALYST_MODE": raw}).mode, "template", raw)

    def test_mode_is_case_insensitive_and_trimmed(self):
        self.assertEqual(AnalystSettings.from_env({"ANALYST_MODE": " CACHED "}).mode, "cached")
        self.assertEqual(AnalystSettings.from_env({"ANALYST_MODE": "Llm"}).mode, "llm")

    def test_numeric_settings_and_bad_values(self):
        s = AnalystSettings.from_env({"ANALYST_DAILY_BUDGET_RUB": "250,5", "ANALYST_MAX_TOKENS": "900", "ANALYST_TIMEOUT_S": "12",
                                      "ANALYST_MAX_RETRIES": "1", "ANALYST_MAX_PARALLEL": "0", "ANALYST_SESSION_CAP_RUB": "3"})
        self.assertEqual((s.daily_budget_rub, s.max_tokens, s.timeout_s, s.max_retries, s.session_cap_rub), (250.5, 900, 12.0, 1, 3.0))
        self.assertEqual(s.max_parallel, 1)  # не меньше одного потока
        bad = AnalystSettings.from_env({"ANALYST_DAILY_BUDGET_RUB": "много", "ANALYST_MAX_TOKENS": "x", "ANALYST_TIMEOUT_S": "-"})
        self.assertEqual((bad.daily_budget_rub, bad.max_tokens, bad.timeout_s), (400.0, 1600, 60.0))

    def test_json_repairs_never_above_two(self):
        self.assertEqual(AnalystSettings.from_env({"ANALYST_MAX_JSON_REPAIRS": "7"}).max_json_repairs, 2)
        self.assertEqual(AnalystSettings.from_env({"ANALYST_MAX_JSON_REPAIRS": "1"}).max_json_repairs, 1)

    def test_default_models_and_budget(self):
        s = AnalystSettings.from_env({})
        self.assertEqual(s.model, "z-ai/glm-5.3-flash")
        self.assertLess(s.daily_budget_rub, 500.0)  # запас до общего лимита команды
        self.assertTrue(s.prefer_cache)
        self.assertTrue(s.check_key_info)
        self.assertTrue(s.feed_hide_excluded)

    def test_urls_are_stripped_of_trailing_slash_and_fallback_can_be_disabled(self):
        s = AnalystSettings.from_env({"LLM_BASE_URL": "https://x.test/v1/ ", "LLM_FALLBACK_BASE_URL": ""})
        self.assertEqual(s.base_url, "https://x.test/v1")
        self.assertEqual(s.fallback_base_url, "")

    def test_with_overrides_is_immutable_copy(self):
        s = AnalystSettings.from_env({})
        s2 = s.with_overrides(mode="cached")
        self.assertEqual((s.mode, s2.mode), ("template", "cached"))

    def test_fixed_as_of(self):
        svc, _, _ = make_service(mode="cached", with_llm=False)
        self.assertEqual(svc.as_of().isoformat(), AS_OF)
        free = AnalystService(AnalystSettings.from_env({"ANALYST_MODE": "cached", "ANALYST_CACHE_DIR": tempfile.mkdtemp()}))
        self.assertIsNotNone(free.as_of().tzinfo)


class TestTemplateMode(UseService):
    def setUp(self):
        super().setUp()
        self.fake = FakeLLM(lambda r: self.fail("в режиме template LLM не вызывается"))
        self.svc = self.use(make_service(mode="template", with_llm=False)[0])
        self.svc.llm = self.fake

    def test_service_is_disabled(self):
        self.assertFalse(self.svc.enabled)
        self.assertFalse(store._analyst_enabled())  # noqa: SLF001
        self.assertEqual(self.svc.live_status(), (False, "режим template"))

    def test_feed_is_the_old_template_feed(self):
        for company in store.get_companies():
            feed = store.get_feed(company.company_id, "cfo")
            raw = next(c for c in store._companies if c["company_id"] == company.company_id)  # noqa: SLF001
            expected = sorted(
                [(ev["event_id"], store._score_event_for_company(ev, raw, "cfo").score) for ev in store.user_events + store._events],  # noqa: SLF001
                key=lambda x: -x[1],
            )
            self.assertEqual([(i.event.event_id, i.relevance.score) for i in feed], expected)
            self.assertEqual(len(feed), 7)  # шаблон показывает все события, ничего не скрывает
        self.assertEqual(self.fake.requests, [])

    def test_insight_is_the_old_template_pipeline(self):
        with mock.patch.object(self.svc.analyst, "analyze_candidate", side_effect=AssertionError("аналитик не должен вызываться")):
            ins = store.get_insight("e1_key_rate", CO, "cfo")
        self.assertEqual([t.tool for t in ins.trace], TEMPLATE_TOOLS)
        self.assertEqual(ins.run_id, f"r_e1_key_rate_{CO}_cfo")
        self.assertEqual(self.fake.requests, [])

    def test_chat_is_template_answer(self):
        answer = store.ask(CO, "e1_key_rate", "cfo", "r1", "Какие риски?")
        self.assertIsInstance(answer, QaAnswer)
        self.assertTrue(answer.did_extra_search)
        self.assertEqual(self.fake.requests, [])

    def test_upload_keeps_default_topic_and_date(self):
        ev = store.ingest_news("Новость про ставку", "Банк России изменил ставку.")
        self.assertEqual((ev.topic, ev.date, ev.user_uploaded), ("other", "2026-10-21", True))
        self.assertEqual(self.fake.requests, [])

    def test_metrics_report_template_and_zero_spend(self):
        m = store.get_metrics()
        self.assertEqual(m["analyst"]["analyst_mode"], "template")
        self.assertIsNone(m["analyst"]["analyst_model"])
        self.assertEqual((m["llm_calls"], m["spent_today_rub"], m["total_tokens_consumed"]), (0, 0.0, 0))

    def test_broken_analyst_init_does_not_break_template_path(self):
        with mock.patch.object(service_mod, "get_analyst_service", side_effect=RuntimeError("не удалось")):
            self.assertFalse(store._analyst_enabled())  # noqa: SLF001
        self.assertEqual(len(store.get_feed(CO, "cfo")), 7)

    def test_analyst_failure_in_llm_mode_falls_back_to_template(self):
        svc = self.use(make_service(mode="cached", with_llm=False, bundled=BUNDLED)[0])
        with mock.patch.object(svc, "feed", side_effect=RuntimeError("сбой")):
            feed = store.get_feed(CO, "cfo")
        self.assertEqual(len(feed), 7)  # откат на шаблон, исключение не доходит до API
        with mock.patch.object(orchestrator, "run_llm_pipeline", side_effect=RuntimeError("сбой")):
            ins = store.get_insight("e1_key_rate", CO, "cfo")
        self.assertEqual([t.tool for t in ins.trace], TEMPLATE_TOOLS)


class TestCachedMode(UseService):
    def make(self, **kw):
        svc = make_service(mode="cached", with_llm=False, bundled=kw.pop("bundled", BUNDLED), **kw)[0]
        return self.use(svc)

    def test_no_gateway_even_with_key_and_no_network_calls(self):
        svc = self.make(extra_env={"LLM_API_KEY": "sk-" + "TESTKEY0123456789abcdefFAKE"})
        self.assertIsNone(svc.llm)
        with NetworkForbidden():
            feed = svc.feed(seed_company(CO), "cfo", [seed_event("e1_key_rate"), seed_event("e3_rub_weak")], score_fn)
            ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("шаблон не нужен"))
        self.assertEqual(len(feed), 2)
        self.assertIn(ins.generated_by, ("cached", "live"))
        self.assertEqual(svc.stats.snapshot()["llm_calls"], 0)
        self.assertEqual(svc.ledger.spent_window_rub, 0.0)

    def test_reads_bundled_cache(self):
        svc = self.make()
        svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("кэш должен сработать"))
        snap = svc.cache.snapshot()
        self.assertGreaterEqual(snap["bundled_hits"], 1)
        self.assertEqual(snap["writes"], 0)  # кэш репозитория и рабочий каталог не пишутся

    def test_first_card_from_bundled_cache_is_marked_cached(self):
        svc = self.make()
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("шаблон не нужен"))
        self.assertEqual(ins.generated_by, "cached")
        step = next(t for t in ins.trace if t.tool == "cache_lookup")
        self.assertIn("бюджет не расходуется", step.summary)
        self.assertIn("Сохранённый ответ из репозитория", step.result)

    def test_result_is_deterministic(self):
        a = self.make().insight(seed_company(CO), "cfo", seed_event("e2_eu_steel_quota"), score_fn, lambda: self.fail("x"))
        b = self.make().insight(seed_company(CO), "cfo", seed_event("e2_eu_steel_quota"), score_fn, lambda: self.fail("x"))
        self.assertEqual(a.run_id, b.run_id)
        self.assertEqual(a.relevance, b.relevance)
        self.assertEqual([c.text for c in a.risks], [c.text for c in b.risks])

    def test_all_precomputed_candidates_are_served(self):
        svc = self.make()
        from app.services.seed_data import COMPANIES_DATA, EVENTS_DATA

        misses = 0
        for company in COMPANIES_DATA:
            for role in ("ceo", "cfo", "treasurer", "chief_accountant"):
                inp = svc.build_input(company, role, EVENTS_DATA, None)
                for cand in inp.candidates:
                    try:
                        svc.analyst.analyze_candidate(inp, cand, allow_live=False)
                    except Exception:  # noqa: BLE001
                        misses += 1
        self.assertEqual(misses, 0, "предрасчитанный кэш должен покрывать 3 компании x 7 событий x 4 роли")

    def test_cache_miss_falls_back_to_template_with_note(self):
        svc = self.make(bundled=Path(tempfile.mkdtemp()))  # пустой кэш
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, template_insight_fn())
        self.assertEqual(ins.trace[-1].tool, "llm_analyst")
        self.assertEqual(ins.trace[-1].status, "warn")
        self.assertIn("шаблонный результат", ins.trace[-1].summary)
        self.assertEqual(svc.fallbacks_to_template, 1)
        self.assertEqual(svc.metrics()["fallbacks_to_template"], 1)

    def test_feed_falls_back_per_candidate_on_miss(self):
        svc = self.make(bundled=Path(tempfile.mkdtemp()))
        feed = svc.feed(seed_company(CO), "cfo", [seed_event("e1_key_rate"), seed_event("e7_noise_sports")], score_fn)
        self.assertEqual({i.event.event_id for i in feed}, {"e1_key_rate", "e7_noise_sports"})
        by_id = {i.event.event_id: i for i in feed}
        self.assertEqual(by_id["e1_key_rate"].relevance.score, score_fn(seed_event("e1_key_rate"), seed_company(CO), "cfo").score)

    def test_other_model_setting_is_served_by_model_independent_lookup(self):
        svc = self.make(extra_env={"ANALYST_MODEL": "qwen/qwen3.6-35b-a3b", "ANALYST_FALLBACK_MODEL": "google/gemma-4-26b-a4b-it"})
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("кэш без учёта модели должен найтись"))
        self.assertEqual(ins.generated_by, "cached")
        self.assertTrue(any(t.tool == "cache_lookup" and "Живой вызов невозможен" in t.summary for t in ins.trace))

    def test_hide_excluded_flag_changes_feed_size_only(self):
        events = [seed_event(e) for e in ("e1_key_rate", "e2_eu_steel_quota", "e3_rub_weak", "e4_wheat_duty", "e5_coke_supplier", "e6_food_inflation", "e7_noise_sports")]
        hidden = self.make(extra_env={"ANALYST_FEED_HIDE_EXCLUDED": "true"}).feed(seed_company(CO), "cfo", events, score_fn)
        shown = self.make(extra_env={"ANALYST_FEED_HIDE_EXCLUDED": "false"}).feed(seed_company(CO), "cfo", events, score_fn)
        self.assertEqual(len(shown), 7)
        self.assertLess(len(hidden), len(shown))
        self.assertTrue({i.event.event_id for i in hidden} < {i.event.event_id for i in shown})
        self.assertIn("e7_noise_sports", {i.event.event_id for i in shown})
        self.assertNotIn("e7_noise_sports", {i.event.event_id for i in hidden})
        same = {i.event.event_id: i.relevance.score for i in hidden}
        for i in shown:
            if i.event.event_id in same:
                self.assertEqual(i.relevance.score, same[i.event.event_id])  # баллы не зависят от флага

    def test_cached_chat_uses_template_answer(self):
        svc = self.make()
        sentinel = QaAnswer(text="шаблонный ответ")
        answer = svc.ask(seed_company(CO), "cfo", seed_event("e1_key_rate"), "Что делать?", score_fn, template_insight_fn(), lambda: sentinel)
        self.assertIs(answer, sentinel)

    def test_upload_classified_by_rules_without_llm(self):
        svc = self.make()
        rates = svc.classify_upload("Банк России изменил ключевую ставку", "Решение по кредитам и депозитам.")
        self.assertEqual((rates["topic"], rates["classified_by"]), ("rates", "rules"))
        fx = svc.classify_upload("Курс юаня вырос", "")
        self.assertEqual(fx["topic"], "fx")
        other = svc.classify_upload("Турнир по шахматам", "Финал пройдёт в выходные.")
        self.assertEqual((other["topic"], other["topic_label"]), ("other", "Пользовательское"))

    def test_metrics_in_cached_mode(self):
        svc = self.make()
        svc.feed(seed_company(CO), "cfo", [seed_event("e1_key_rate")], score_fn)
        m = svc.metrics()
        self.assertEqual(m["analyst_mode"], "cached")
        self.assertEqual(m["analyst_model"], "z-ai/glm-5.3-flash")
        self.assertFalse(m["live_llm_available"])
        self.assertEqual(m["live_llm_status"], "режим cached")
        self.assertEqual(m["insights_analyzed"], 1)
        self.assertEqual(m["claim_source_attribution_ratio"], 1.0)
        self.assertEqual(m["llm"]["llm_calls"], 0)
        self.assertEqual(m["budget"]["spent_window_rub"], 0.0)
        self.assertEqual(m["budget"]["reset_at"], "03:00 MSK")


class TestLlmMode(UseService):
    def test_without_key_degrades_to_cache_then_template(self):
        svc = self.use(make_service(mode="llm", with_llm=False, bundled=BUNDLED)[0])
        self.assertIsNone(svc.llm)
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("кэш должен сработать"))
        self.assertEqual(ins.generated_by, "cached")
        empty = self.use(make_service(mode="llm", with_llm=False)[0])
        ins2 = empty.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, template_insight_fn())
        self.assertEqual(ins2.trace[-1].status, "warn")
        m = empty.metrics()
        self.assertEqual((m["analyst_mode"], m["live_llm_available"]), ("llm", False))

    def test_prefer_cache_serves_bundled_answers_without_llm_calls(self):
        svc, fake, _ = make_service(mode="llm", bundled=BUNDLED)
        self.use(svc)
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("x"))
        self.assertEqual(fake.requests, [])
        self.assertEqual(ins.generated_by, "cached")

    def test_prefer_cache_off_calls_llm_and_writes_runtime_cache(self):
        tmp = Path(tempfile.mkdtemp(prefix="analyst_modes_"))
        svc, fake, _ = make_service(mode="llm", bundled=BUNDLED, tmp=tmp, extra_env={"ANALYST_PREFER_CACHE": "false"})
        self.use(svc)
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("x"))
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(ins.generated_by, "live")
        files = list((tmp / "runtime").rglob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn("sk-", files[0].read_text(encoding="utf-8"))
        # повторный запрос и новый процесс (новый сервис с тем же рабочим каталогом) не тратят бюджет
        svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("x"))
        self.assertEqual(len(fake.requests), 1)
        svc2, fake2, _ = make_service(mode="llm", bundled=BUNDLED, tmp=tmp, extra_env={"ANALYST_PREFER_CACHE": "false"})
        svc2.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("x"))
        self.assertEqual(fake2.requests, [])

    def test_live_call_records_usage_and_metrics(self):
        svc, fake, _ = make_service(mode="llm")
        self.use(svc)
        svc.feed(seed_company(CO), "cfo", [seed_event("e1_key_rate"), seed_event("e3_rub_weak")], score_fn)
        m = svc.metrics()
        self.assertTrue(m["live_llm_available"])
        self.assertEqual(m["llm"]["llm_calls"], 2)
        self.assertGreater(m["llm"]["prompt_tokens"], 0)
        self.assertGreater(m["budget"]["spent_window_rub"], 0.0)
        self.assertAlmostEqual(m["budget"]["spent_window_rub"], m["llm"]["cost_rub"], places=3)
        self.assertEqual(m["insights_analyzed"], 2)

    def test_llm_failure_uses_stored_answer_or_template(self):
        fake = FakeLLM([LLMUnavailable("шлюз недоступен")] * 4, ledger=BudgetLedger(400.0))
        svc, _, _ = make_service(mode="llm", with_llm=False, bundled=BUNDLED, extra_env={"ANALYST_PREFER_CACHE": "false"})
        svc.llm = fake
        svc.analyst.llm = fake
        self.use(svc)
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("есть сохранённый ответ"))
        self.assertTrue(fake.requests)
        self.assertEqual(ins.generated_by, "cached")
        self.assertTrue(any(t.status == "warn" and "Живой вызов невозможен" in t.summary for t in ins.trace))
        # без сохранённого ответа — шаблон
        svc2, _, _ = make_service(mode="llm", with_llm=False)
        fake2 = FakeLLM([LLMUnavailable("нет")] * 4)
        svc2.llm = fake2
        svc2.analyst.llm = fake2
        ins2 = svc2.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, template_insight_fn())
        self.assertEqual(ins2.trace[-1].tool, "llm_analyst")

    def test_invalid_json_three_times_degrades(self):
        fake = FakeLLM(lambda r: "это не json")
        svc, _, _ = make_service(mode="llm", with_llm=False)
        svc.llm = fake
        svc.analyst.llm = fake
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, template_insight_fn())
        self.assertEqual(ins.trace[-1].status, "warn")
        self.assertLessEqual(len(fake.requests), 6)  # по 3 запроса (1 + 2 ремонта) на основную и запасную модель
        self.assertEqual(svc.fallbacks_to_template, 1)

    def test_fallback_model_used_when_primary_fails(self):
        def respond(req: ChatRequest):
            if req.model == "z-ai/glm-5.3-flash":
                raise LLMUnavailable("основная модель недоступна")
            return json.dumps(draft(), ensure_ascii=False)

        fake = FakeLLM(respond)
        svc, _, _ = make_service(mode="llm", with_llm=False)
        svc.llm = fake
        svc.analyst.llm = fake
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("x"))
        self.assertEqual([r.model for r in fake.requests], ["z-ai/glm-5.3-flash", "deepseek/deepseek-v4.1-flash"])
        self.assertEqual(ins.generated_by, "live")

    def test_budget_exhausted_does_not_try_other_model(self):
        fake = FakeLLM(lambda r: json.dumps(draft()), ledger=BudgetLedger(0.0))
        svc, _, _ = make_service(mode="llm", with_llm=False)
        svc.llm = fake
        svc.analyst.llm = fake
        svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, template_insight_fn())
        self.assertEqual(len(fake.requests), 1)  # отказ по бюджету не ведёт к перебору запасных моделей
        self.assertEqual(fake.stats.snapshot()["llm_calls"], 0)  # и ничего не списано

    def test_only_allowed_models_ever_requested(self):
        from tests.analyst.test_gateway import ALLOWED_MODELS

        svc, fake, _ = make_service(mode="llm")
        svc.feed(seed_company(CO), "cfo", [seed_event("e1_key_rate"), seed_event("e2_eu_steel_quota")], score_fn)
        self.assertTrue(fake.requests)
        self.assertTrue({r.model for r in fake.requests} <= ALLOWED_MODELS)

    def test_chat_goes_through_llm_and_is_cached(self):
        def respond(req: ChatRequest):
            if req.purpose == "chat":
                return json.dumps({"answer": "Ставка влияет на стоимость нового кредитования.", "evidence_ids": ["n_e1_1", "ghost"], "insufficient": False}, ensure_ascii=False)
            return json.dumps(draft(), ensure_ascii=False)

        svc, fake, _ = make_service(mode="llm", responder=respond)
        company, event = seed_company(CO), seed_event("e1_key_rate")
        template_called = mock.Mock(side_effect=AssertionError("шаблонный ответ не нужен"))
        a1 = svc.ask(company, "cfo", event, "Что это значит для кредитов?", score_fn, template_insight_fn(), template_called)
        self.assertEqual(a1.evidence_ids, ["n_e1_1"])  # выдуманный ID отброшен
        self.assertFalse(a1.insufficient)
        self.assertFalse(a1.from_cache)
        self.assertEqual([e.evidence_id for e in a1.evidence], ["n_e1_1"])
        calls = len(fake.requests)
        a2 = svc.ask(company, "cfo", event, "  что ЭТО значит для кредитов?  ", score_fn, template_insight_fn(), template_called)
        self.assertTrue(a2.from_cache)  # тот же вопрос с другим регистром/пробелами — из кэша
        self.assertEqual(len(fake.requests), calls)

    def test_chat_unsupported_numbers_and_missing_sources_mark_insufficient(self):
        def respond(req: ChatRequest):
            if req.purpose == "chat":
                return json.dumps({"answer": "Расходы вырастут на 777 млн ₽.", "evidence_ids": ["n_e1_1"], "insufficient": False}, ensure_ascii=False)
            return json.dumps(draft(), ensure_ascii=False)

        svc, _, _ = make_service(mode="llm", responder=respond)
        a = svc.ask(seed_company(CO), "cfo", seed_event("e1_key_rate"), "Сколько потеряем?", score_fn, template_insight_fn(), lambda: self.fail("x"))
        self.assertTrue(a.insufficient)
        self.assertIn("не подтверждена", a.text)

        def respond2(req: ChatRequest):
            if req.purpose == "chat":
                return json.dumps({"answer": "Данных нет.", "evidence_ids": [], "insufficient": False}, ensure_ascii=False)
            return json.dumps(draft(), ensure_ascii=False)

        svc2, _, _ = make_service(mode="llm", responder=respond2)
        a2 = svc2.ask(seed_company(CO), "cfo", seed_event("e1_key_rate"), "Что с ЭБИТДА?", score_fn, template_insight_fn(), lambda: self.fail("x"))
        self.assertTrue(a2.insufficient)  # ответ без источников не выдаётся за уверенный

    def test_chat_llm_error_falls_back_to_template_answer(self):
        def respond(req: ChatRequest):
            if req.purpose == "chat":
                raise LLMUnavailable("чат недоступен")
            return json.dumps(draft(), ensure_ascii=False)

        svc, _, _ = make_service(mode="llm", responder=respond)
        sentinel = QaAnswer(text="шаблон")
        a = svc.ask(seed_company(CO), "cfo", seed_event("e1_key_rate"), "Вопрос?", score_fn, template_insight_fn(), lambda: sentinel)
        self.assertIs(a, sentinel)

    def test_chat_card_for_model_has_sources_and_no_internal_ids(self):
        svc, fake, _ = make_service(mode="llm")
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("x"))
        card = AnalystService.card_for_chat(ins)
        self.assertEqual(card["event"]["title"], ins.event.title)
        self.assertTrue(card["sources"])
        self.assertIn("what_happened", card)
        json.dumps(card, ensure_ascii=False)  # сериализуется для промпта

    def test_classify_upload_with_llm_and_fallback_to_rules(self):
        def respond(req: ChatRequest):
            if req.purpose == "classify":
                return json.dumps({"topic": "fx", "business_relevant": True, "summary": "Курс изменился."}, ensure_ascii=False)
            return json.dumps(draft(), ensure_ascii=False)

        svc, fake, _ = make_service(mode="llm", responder=respond)
        res = svc.classify_upload("Курс изменился", "Подробности.")
        self.assertEqual((res["topic"], res["classified_by"], res["summary"]), ("fx", "llm", "Курс изменился."))
        self.assertEqual(res["topic_label"], "Валюта")
        broken = FakeLLM([LLMUnavailable("нет")])
        svc.llm = broken
        svc.analyst.llm = broken
        res2 = svc.classify_upload("Решение по ставке", "")
        self.assertEqual((res2["topic"], res2["classified_by"]), ("rates", "rules"))

    def test_runtime_dir_unwritable_does_not_break_service(self):
        tmp = Path(tempfile.mkdtemp())
        blocker = tmp / "file_instead_of_dir"
        blocker.write_text("x", encoding="utf-8")
        svc, _, _ = make_service(mode="llm", tmp=tmp, extra_env={"ANALYST_CACHE_DIR": str(blocker / "sub")})
        ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("x"))
        self.assertEqual(ins.status, "ok")


class TestModesProduceSameShape(UseService):
    def test_insight_shape_is_identical_across_modes(self):
        shapes = {}
        template = self.use(make_service(mode="template", with_llm=False)[0])
        for mode, svc in (("llm", make_service(mode="llm")[0]), ("cached", make_service(mode="cached", with_llm=False, bundled=BUNDLED)[0])):
            ins = svc.insight(seed_company(CO), "cfo", seed_event("e1_key_rate"), score_fn, lambda: self.fail("x"))
            Insight.model_validate(json.loads(ins.model_dump_json()))
            shapes[mode] = set(ins.model_dump())
        tpl = orchestrator.run_pipeline(event=seed_event("e1_key_rate"), company=seed_company(CO), role="cfo", score_fn=score_fn)
        shapes["template"] = set(tpl.model_dump())
        self.assertEqual(shapes["llm"], shapes["template"])
        self.assertEqual(shapes["cached"], shapes["template"])
        self.assertFalse(template.enabled)


if __name__ == "__main__":
    unittest.main()
