"""Сквозные тесты HTTP API (FastAPI TestClient, без сети и без расхода бюджета).

Режимы ``template`` (прежнее поведение) и ``cached`` (сохранённые ответы аналитика из репозитория),
плюс ``llm`` на ``FakeLLM``. Формы ответов сверяются с ``docs/frontend-api-contract.md`` и со схемами
Pydantic ``app/schemas/base.py``. Сервис аналитика подменяется явно — ``backend/.env`` не читается.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import warnings
from pathlib import Path
from typing import Any

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from app.analyst import service as service_mod
from app.analyst.analyst import level_from_score
from app.analyst.llm_gateway import ChatRequest
from app.analyst.settings import PACKAGE_DIR
from app.schemas.base import CompanyProfile, FeedItem, Insight, NewsEvent, QaAnswer
from app.services.store import store
from main import app

from tests.analyst.helpers import AS_OF, draft, make_service

API = "/api/v1"
BUNDLED = PACKAGE_DIR / "cache"
COMPANIES = ["SYNTHETIC_U_1042", "SYNTHETIC_U_2218", "SYNTHETIC_U_3305"]
ROLES = ["ceo", "cfo", "treasurer", "chief_accountant"]
EVENT_IDS = ["e1_key_rate", "e2_eu_steel_quota", "e3_rub_weak", "e4_wheat_duty", "e5_coke_supplier", "e6_food_inflation", "e7_noise_sports"]
PRODUCT_KEYS = {"rko", "guarantees", "loans", "leasing", "deposits", "payroll", "factoring"}
TOPICS = {"rates", "sanctions", "fx", "trade", "inflation", "counterparty", "industry", "other"}
EVIDENCE_TYPES = {"news", "bank_research", "company_feature", "synthetic"}
TEST_KEY = "sk-" + "TESTKEY0123456789abcdefFAKEFAKE"


class ApiCase(unittest.TestCase):
    """Подменяет сервис аналитика и состояние store, возвращает всё назад."""

    def setUp(self):
        self._prev_service = service_mod._service
        self._prev_events = list(store.user_events)
        self.addCleanup(self._restore)
        self.client = TestClient(app)  # без `with`: lifespan (создание БД) не запускается

    def _restore(self):
        service_mod.set_analyst_service(self._prev_service)
        store.user_events[:] = self._prev_events

    def use_mode(self, mode: str, *, hide_excluded: bool = True, extra_env: dict[str, str] | None = None, responder: Any = None):
        env = {"ANALYST_FEED_HIDE_EXCLUDED": "true" if hide_excluded else "false"}
        env.update(extra_env or {})
        if mode == "llm":
            svc, fake, _ = make_service(mode="llm", responder=responder, extra_env=env)
        else:
            svc, fake, _ = make_service(mode=mode, with_llm=False, bundled=BUNDLED, extra_env=env)
        service_mod.set_analyst_service(svc)
        self.svc, self.fake = svc, fake
        return svc

    def get(self, path: str, **params):
        r = self.client.get(API + path, params=params or None)
        return r

    def feed(self, company: str, role: str = "cfo"):
        r = self.get(f"/companies/{company}/news-feed", role=role)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def analysis(self, event_id: str, company: str = "SYNTHETIC_U_1042", role: str = "cfo"):
        r = self.get(f"/news/{event_id}/analysis", company_id=company, role=role)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


# --------------------------------------------------------------------------- проверки контракта


def is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def check_company(tc: unittest.TestCase, c: dict[str, Any]) -> None:
    for key, typ in (("company_id", str), ("name", str), ("industry", str), ("region", str), ("age_label", str), ("profile_version", str)):
        tc.assertIsInstance(c[key], typ, key)
        tc.assertTrue(c[key], key)
    tc.assertTrue(is_num(c["revenue_bn_rub"]))
    tc.assertIs(c["is_synthetic"], True)
    tc.assertEqual(set(c["products"]), PRODUCT_KEYS)
    for k, v in c["products"].items():
        tc.assertIn(v, (True, False, None), k)  # неизвестное — null, а не false/0 по умолчанию
    tc.assertEqual(set(c["fts"]), {"flag", "country"})
    tc.assertIn(c["fts"]["flag"], (True, False, None))
    CompanyProfile.model_validate(c)


def check_event(tc: unittest.TestCase, e: dict[str, Any]) -> None:
    for key in ("event_id", "title", "date", "topic_label"):
        tc.assertIsInstance(e[key], str)
        tc.assertTrue(e[key], key)
    tc.assertIsInstance(e["sources_count"], int)
    tc.assertIsInstance(e["is_synthetic"], bool)
    tc.assertIn(e["topic"], TOPICS)
    tc.assertGreaterEqual(len(e["date"]), 10)  # фронт берёт первые 10 символов (YYYY-MM-DD)
    tc.assertRegex(e["date"][:10], r"^\d{4}-\d{2}-\d{2}$")
    NewsEvent.model_validate(e)


def check_relevance(tc: unittest.TestCase, r: dict[str, Any], *, sum_matches: bool = False) -> None:
    tc.assertTrue(is_num(r["score"]))
    tc.assertGreaterEqual(r["score"], 0)
    tc.assertLessEqual(r["score"], 1)
    tc.assertIn(r["level"], ("high", "medium", "low"))  # других значений нет
    tc.assertEqual(r["level"], level_from_score(r["score"]), "уровень должен соответствовать порогам 0,50 / 0,25")
    tc.assertIsInstance(r["explanation"], str)
    tc.assertTrue(r["explanation"])
    tc.assertIsInstance(r["factors"], list)
    for f in r["factors"]:
        for key in ("feature_key", "label", "detail"):
            tc.assertIsInstance(f[key], str)
        tc.assertTrue(is_num(f["contribution"]))
    if sum_matches and r["factors"]:
        tc.assertAlmostEqual(sum(f["contribution"] for f in r["factors"]), r["score"], delta=0.011)
    if r.get("reasons") is not None:
        tc.assertTrue(all(isinstance(x, str) for x in r["reasons"]))


def check_feed_item(tc: unittest.TestCase, item: dict[str, Any], *, sum_matches: bool = False) -> None:
    tc.assertEqual({"event", "relevance", "why_short", "has_bank_position", "profile_version"}, set(item))
    check_event(tc, item["event"])
    check_relevance(tc, item["relevance"], sum_matches=sum_matches)
    tc.assertIsInstance(item["why_short"], str)
    tc.assertTrue(item["why_short"])
    tc.assertIsInstance(item["has_bank_position"], bool)
    tc.assertIsInstance(item["profile_version"], str)
    FeedItem.model_validate(item)


def check_claim(tc: unittest.TestCase, c: dict[str, Any], *, ids: set[str], strict_features: bool = True) -> None:
    tc.assertIsInstance(c["text"], str)
    tc.assertTrue(c["text"])
    tc.assertIsInstance(c["evidence_ids"], list)
    for e in c["evidence_ids"]:
        tc.assertIn(e, ids, "evidence_id в утверждении должен быть в списке evidence")
    if c.get("kind") is not None:
        tc.assertIn(c["kind"], ("fact", "inference", "hypothesis"))
    for f in c.get("features") or [] if strict_features else []:
        tc.assertIn(f"cf:{f}", ids, "признак клиента должен быть в evidence с id cf:<feature_id>")
    if c.get("roles"):
        tc.assertTrue(set(c["roles"]) <= set(ROLES))
    if c.get("kind") == "fact":  # «fact» допустим только для утверждения, подтверждённого источником
        tc.assertTrue(c["evidence_ids"], c["text"])


def check_insight(tc: unittest.TestCase, j: dict[str, Any], *, company: str, role: str, sum_matches: bool = False,
                  strict_features: bool = True) -> None:
    expected = {"run_id", "company_id", "profile_version", "user_role", "status", "generated_by", "event", "relevance", "what_happened",
                "why_it_matters", "risks", "opportunities", "next_actions", "impact_estimate", "bank_position", "ai_conclusion",
                "unknowns", "evidence", "trace"}
    tc.assertEqual(set(j), expected)
    tc.assertEqual((j["company_id"], j["user_role"]), (company, role))
    tc.assertIn(j["status"], ("ok", "insufficient_data"))
    tc.assertIn(j["generated_by"], ("live", "cached"))
    tc.assertTrue(j["run_id"])
    check_event(tc, j["event"])
    check_relevance(tc, j["relevance"], sum_matches=sum_matches)

    ids = {e["evidence_id"] for e in j["evidence"]}
    tc.assertEqual(len(ids), len(j["evidence"]), "evidence_id уникальны")
    for e in j["evidence"]:
        tc.assertEqual({"evidence_id", "type", "title", "publisher", "date", "quote", "is_synthetic"} - set(e), set())
        tc.assertIn(e["type"], EVIDENCE_TYPES)
        tc.assertTrue(e["quote"] and e["title"] and e["publisher"])
        tc.assertIsInstance(e["is_synthetic"], bool)
        if e["type"] == "company_feature":
            tc.assertTrue(e["evidence_id"].startswith("cf:"))

    tc.assertIsInstance(j["what_happened"]["summary"], str)
    for section in (j["what_happened"]["facts"], j["why_it_matters"], j["risks"], j["opportunities"], j["next_actions"]):
        for c in section:
            check_claim(tc, c, ids=ids, strict_features=strict_features)
    for c in j["what_happened"]["facts"]:
        tc.assertIn(c.get("kind"), ("fact", None))
    for c in j["next_actions"]:
        tc.assertNotEqual(c.get("kind"), "fact")  # шаг — рекомендация, а не факт

    est = j["impact_estimate"]
    if est is not None:
        tc.assertEqual(j["relevance"]["level"], "high", "оценка эффекта только при высокой значимости")
        tc.assertEqual({"metric", "summary", "assumptions", "evidence_ids"}, set(est))
        tc.assertTrue(est["assumptions"], "оценка эффекта — сценарий с допущениями")

    bank = j["bank_position"]
    if bank is not None:
        tc.assertTrue(bank["claims"])
        for c in bank["claims"]:
            tc.assertIn(c["evidence_ids"][0], {e["evidence_id"] for e in j["evidence"] if e["type"] == "bank_research"})
    ai = j["ai_conclusion"]
    if ai is not None:
        tc.assertTrue(ai["text"])
        if bank is not None:
            tc.assertNotIn(ai["text"], [c["text"] for c in bank["claims"]])  # позиция банка и вывод AI — разные блоки

    for u in j["unknowns"]:
        tc.assertTrue(u["text"])
    tc.assertEqual([t["n"] for t in j["trace"]], list(range(1, len(j["trace"]) + 1)))
    for t in j["trace"]:
        tc.assertIn(t["status"], ("ok", "retry", "warn"))
        tc.assertTrue(t["tool"] and t["summary"] and t["result"] is not None)
    Insight.model_validate(j)


def check_answer(tc: unittest.TestCase, a: dict[str, Any]) -> None:
    for key in ("text", "evidence", "did_extra_search", "from_cache", "insufficient"):
        tc.assertIn(key, a)
    tc.assertTrue(a["text"])
    tc.assertIsInstance(a["did_extra_search"], bool)
    tc.assertIsInstance(a["from_cache"], bool)
    tc.assertIsInstance(a["insufficient"], bool)
    ev_ids = {e["evidence_id"] for e in a["evidence"]}
    for e in a["evidence"]:
        tc.assertIn(e["type"], EVIDENCE_TYPES)
        tc.assertTrue(e["quote"])
    tc.assertTrue(set(a.get("evidence_ids") or []) <= ev_ids | set(a.get("evidence_ids") or []))
    QaAnswer.model_validate(a)


# --------------------------------------------------------------------------- общие для режимов проверки


class CommonApiChecks:
    """Миксин: одинаковые требования к формам для template и cached."""

    mode = "template"
    hide_excluded = False
    strict_features = True  # в шаблонном режиме признаки клиента в evidence названы иначе (старое поведение)

    def setUp(self):  # noqa: N802
        super().setUp()
        self.use_mode(self.mode, hide_excluded=self.hide_excluded)

    def test_root_health(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_companies(self):
        r = self.get("/companies")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual([c["company_id"] for c in data], COMPANIES)
        for c in data:
            check_company(self, c)

    def test_feed_shape_all_companies_and_roles(self):
        for company in COMPANIES:
            for role in ROLES:
                feed = self.feed(company, role)
                self.assertTrue(feed, (company, role))
                for item in feed:
                    check_feed_item(self, item)
                scores = [i["relevance"]["score"] for i in feed]
                self.assertEqual(scores, sorted(scores, reverse=True), "лента отсортирована: самое значимое первым")
                ids = [i["event"]["event_id"] for i in feed]
                self.assertEqual(len(ids), len(set(ids)))

    def test_feed_default_role_is_cfo(self):
        r = self.get(f"/companies/{COMPANIES[0]}/news-feed")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), self.feed(COMPANIES[0], "cfo"))

    def test_feed_has_at_least_six_events_for_first_company_when_not_hidden(self):
        first = self.get("/companies").json()[0]["company_id"]
        for role in ("cfo", "ceo", "treasurer"):
            self.assertGreaterEqual(len(self.feed(first, role)), 6)  # то же ждёт backend/test_e2e.py

    def test_analysis_shape_all_events(self):
        for company in COMPANIES:
            for event_id in EVENT_IDS:
                j = self.analysis(event_id, company, "cfo")
                check_insight(self, j, company=company, role="cfo", strict_features=self.strict_features)
                self.assertEqual(j["event"]["event_id"], event_id)

    def test_analysis_for_every_role(self):
        for role in ROLES:
            check_insight(self, self.analysis("e1_key_rate", COMPANIES[0], role), company=COMPANIES[0], role=role, strict_features=self.strict_features)

    def test_analysis_card_matches_feed_item(self):
        feed = {i["event"]["event_id"]: i for i in self.feed(COMPANIES[0], "cfo")}
        for event_id, item in feed.items():
            if event_id == "e7_noise_sports" and self.mode == "template":
                continue
            card = self.analysis(event_id, COMPANIES[0], "cfo")
            self.assertEqual(card["event"], item["event"])
            self.assertEqual(card["profile_version"], item["profile_version"])
            self.assertEqual(card["relevance"]["level"], item["relevance"]["level"])

    def test_chat_ask(self):
        card = self.analysis("e1_key_rate")
        r = self.client.post(f"{API}/chat/ask", json={"company_id": COMPANIES[0], "news_id": "e1_key_rate", "role": "cfo",
                                                      "run_id": card["run_id"], "question": "Каковы риски изменения ставки?"})
        self.assertEqual(r.status_code, 200, r.text)
        check_answer(self, r.json())

    def test_chat_ask_validation(self):
        base = {"company_id": COMPANIES[0], "news_id": "e1_key_rate", "role": "cfo", "run_id": "r", "question": "?"}
        for broken in ({**base, "role": "boss"}, {k: v for k, v in base.items() if k != "question"}, {**base, "question": None}):
            self.assertEqual(self.client.post(f"{API}/chat/ask", json=broken).status_code, 422, broken)

    def test_upload_news_roundtrip(self):
        r = self.client.post(f"{API}/news", json={"title": "Новое регуляторное требование по раскрытию рисков",
                                                  "text": "ЦБ опубликовал указание о правилах раскрытия чувствительной информации."})
        self.assertEqual(r.status_code, 201, r.text)
        event = r.json()
        check_event(self, event)
        self.assertIs(event["user_uploaded"], True)
        self.assertEqual(event["title"], "Новое регуляторное требование по раскрытию рисков")
        for role in ("cfo", "ceo"):
            feed = self.feed(COMPANIES[0], role)
            item = next((i for i in feed if i["event"]["event_id"] == event["event_id"]), None)
            self.assertIsNotNone(item, "загруженная новость должна сразу появиться в ленте")
            check_feed_item(self, item)
        card = self.analysis(event["event_id"], COMPANIES[0], "cfo")
        check_insight(self, card, company=COMPANIES[0], role="cfo", strict_features=self.strict_features)
        self.assertEqual(card["event"]["event_id"], event["event_id"])

    def test_upload_news_without_text_and_validation(self):
        r = self.client.post(f"{API}/news", json={"title": "Только заголовок"})
        self.assertEqual(r.status_code, 201)
        self.assertIs(r.json()["user_uploaded"], True)
        self.assertEqual(self.client.post(f"{API}/news", json={"text": "без заголовка"}).status_code, 422)
        self.assertEqual(self.client.post(f"{API}/news", content="не json").status_code, 422)

    def test_uploaded_ids_are_unique(self):
        a = self.client.post(f"{API}/news", json={"title": "Первая"}).json()
        b = self.client.post(f"{API}/news", json={"title": "Вторая"}).json()
        self.assertNotEqual(a["event_id"], b["event_id"])

    def test_feedback(self):
        r = self.client.post(f"{API}/feedback", json={"run_id": "r1", "useful": True})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.content, b"")
        self.assertEqual(self.client.post(f"{API}/feedback", json={"run_id": "r1"}).status_code, 422)

    def test_query_validation(self):
        self.assertEqual(self.get(f"/companies/{COMPANIES[0]}/news-feed", role="boss").status_code, 422)
        self.assertEqual(self.get("/news/e1_key_rate/analysis", role="cfo").status_code, 422)  # нет company_id
        self.assertEqual(self.get("/news/e1_key_rate/analysis", company_id=COMPANIES[0], role="janitor").status_code, 422)

    def test_metrics_shape_and_no_secrets(self):
        self.feed(COMPANIES[0])
        r = self.get("/metrics")
        self.assertEqual(r.status_code, 200)
        m = r.json()
        for key in ("service_status", "total_companies_loaded", "total_events_indexed", "claim_source_attribution_ratio", "average_latency_ms",
                    "estimated_cost_per_insight_rub", "compliance_disclaimer", "daily_budget_limit_rub", "spent_today_rub",
                    "budget_remaining_rub", "total_tokens_consumed", "llm_calls", "reset_at", "analyst"):
            self.assertIn(key, m)
        self.assertEqual(m["service_status"], "healthy")
        self.assertEqual(m["total_companies_loaded"], 3)
        self.assertEqual(m["total_events_indexed"], 7)
        self.assertEqual(m["reset_at"], "03:00 MSK")
        self.assertLessEqual(m["analyst_ceiling_rub"], m["daily_budget_limit_rub"])
        self.assertEqual(m["analyst"]["analyst_mode"], self.mode)
        self.assertEqual(m["llm_calls"], 0)  # живых вызовов нет
        self.assertEqual(m["spent_today_rub"], 0.0)
        self.assertEqual(m["total_tokens_consumed"], 0)
        for key in ("claim_source_attribution_ratio", "estimated_cost_per_insight_rub"):
            if m[key] is not None:
                self.assertTrue(is_num(m[key]))
        text = json.dumps(m, ensure_ascii=False)
        self.assertNotIn("sk-", text)
        self.assertNotIn("api_key", text.lower())
        self.assertNotIn("Authorization", text)


class TestTemplateModeApi(CommonApiChecks, ApiCase):
    mode = "template"
    strict_features = False

    def test_feed_contains_all_seven_events(self):
        for company in COMPANIES:
            self.assertEqual(sorted(i["event"]["event_id"] for i in self.feed(company)), sorted(EVENT_IDS))

    def test_old_behaviour_trace_and_ids(self):
        j = self.analysis("e1_key_rate")
        self.assertEqual(j["run_id"], "r_e1_key_rate_SYNTHETIC_U_1042_cfo")
        self.assertEqual([t["tool"] for t in j["trace"]][:3], ["profile_fetcher", "relevance_scorer", "bank_rag_search"])
        self.assertEqual(j["generated_by"], "live")

    def test_no_llm_involved(self):
        self.feed(COMPANIES[0])
        self.analysis("e1_key_rate")
        m = self.get("/metrics").json()
        self.assertEqual(m["analyst"]["llm"]["llm_calls"], 0)
        self.assertEqual(m["analyst"]["analyst_model"], None)
        self.assertEqual(m["analyst"]["insights_analyzed"], 0)

    def test_metrics_count_template_runs(self):
        before = self.get("/metrics").json()
        self.analysis("e1_key_rate")
        after = self.get("/metrics").json()
        self.assertEqual(after["template_runs"], before["template_runs"] + 1)
        self.assertEqual(after["llm_runs"], before["llm_runs"])  # счётчики оркестратора общие на процесс — сравниваем приращение

    def test_upload_keeps_template_classification(self):
        event = self.client.post(f"{API}/news", json={"title": "Ставка выросла", "text": "Банк России повысил ставку."}).json()
        self.assertEqual((event["topic"], event["topic_label"], event["date"]), ("other", "Пользовательское", "2026-10-21"))


class TestCachedModeApi(CommonApiChecks, ApiCase):
    mode = "cached"

    def test_analysis_shape_with_consistent_factor_sums(self):
        for event_id in EVENT_IDS[:6]:
            j = self.analysis(event_id, COMPANIES[0], "cfo")
            if j["generated_by"] in ("cached", "live") and any(t["tool"] == "qa_validator" for t in j["trace"]):
                check_relevance(self, j["relevance"], sum_matches=True)

    def test_cached_trace_shows_analyst_pipeline(self):
        j = self.analysis("e1_key_rate")
        tools = [t["tool"] for t in j["trace"]]
        for step in ("input_adapter", "bank_rag_search", "llm_analyst", "qa_validator"):
            self.assertIn(step, tools)
        self.assertTrue(j["run_id"].startswith("an_"))
        self.assertTrue(j["evidence"])

    def test_first_card_is_marked_cached(self):
        j = self.analysis("e2_eu_steel_quota")  # до обращения к ленте: ответ из репозитория
        self.assertEqual(j["generated_by"], "cached")
        self.assertIn("cache_lookup", [t["tool"] for t in j["trace"]])

    def test_no_network_and_no_spend(self):
        from tests.analyst.test_modes import NetworkForbidden

        with NetworkForbidden():
            for company in COMPANIES:
                self.feed(company)
            self.analysis("e1_key_rate")
            self.client.post(f"{API}/chat/ask", json={"company_id": COMPANIES[0], "news_id": "e1_key_rate", "role": "cfo", "run_id": "r", "question": "?"})
        m = self.get("/metrics").json()
        self.assertEqual((m["llm_calls"], m["spent_today_rub"]), (0, 0.0))
        self.assertGreater(m["analyst"]["cache"]["hit_ratio_pct"], 0)
        self.assertGreater(m["analyst"]["insights_analyzed"], 0)
        self.assertEqual(m["analyst"]["claim_source_attribution_ratio"], 1.0)

    def test_metrics_mark_cached_mode(self):
        self.analysis("e1_key_rate")
        m = self.get("/metrics").json()
        self.assertEqual(m["analyst"]["analyst_model"], "z-ai/glm-5.3-flash")
        self.assertFalse(m["analyst"]["live_llm_available"])
        self.assertGreaterEqual(m["llm_runs"], 1)

    def test_upload_is_classified_by_rules_and_dated_now(self):
        event = self.client.post(f"{API}/news", json={"title": "Банк России изменил ключевую ставку", "text": "Решение по кредитам."}).json()
        self.assertEqual(event["topic"], "rates")
        self.assertGreater(len(event["date"]), 10)  # ISO-время с поясом
        self.assertIn("+03:00", event["date"])

    def test_uploaded_news_stays_in_feed_even_when_excluded_hidden(self):
        self.use_mode("cached", hide_excluded=True)
        event = self.client.post(f"{API}/news", json={"title": "Городской турнир по шахматам", "text": "Финал турнира пройдёт в выходные."}).json()
        ids = [i["event"]["event_id"] for i in self.feed(COMPANIES[0])]
        self.assertIn(event["event_id"], ids)

    def test_uploaded_news_card_is_valid_and_not_sent_to_llm(self):
        # ANALYST_AS_OF зафиксирован в прошлом, а загруженная новость датируется «сейчас»: аналитик честно
        # исключает её проверкой времени (`precheck`), карточка остаётся валидной и не тратит бюджет.
        event = self.client.post(f"{API}/news", json={"title": "Минфин обсуждает новый налог", "text": "Подробности обсуждаются."}).json()
        j = self.analysis(event["event_id"], COMPANIES[1], "treasurer")
        check_insight(self, j, company=COMPANIES[1], role="treasurer")
        self.assertTrue(any(t["tool"] == "precheck" and t["status"] == "warn" for t in j["trace"]))
        self.assertEqual(j["relevance"]["level"], "low")
        self.assertEqual(self.get("/metrics").json()["llm_calls"], 0)

    def test_chat_in_cached_mode_is_valid_and_offline(self):
        r = self.client.post(f"{API}/chat/ask", json={"company_id": COMPANIES[0], "news_id": "e1_key_rate", "role": "cfo", "run_id": "r",
                                                      "question": "Что делать финансовому директору?"})
        a = r.json()
        check_answer(self, a)
        self.assertFalse(a["from_cache"])


class TestHideExcludedFlagApi(ApiCase):
    """Флаг ANALYST_FEED_HIDE_EXCLUDED: скрывает события decision=exclude, не меняя остальную ленту."""

    def test_hidden_by_default_in_cached_mode(self):
        self.use_mode("cached", hide_excluded=True)
        sizes = {}
        for company in COMPANIES:
            feed = self.feed(company)
            sizes[company] = len(feed)
            self.assertNotIn("e7_noise_sports", [i["event"]["event_id"] for i in feed])
        self.assertTrue(all(n < 7 for n in sizes.values()), sizes)
        self.assertIn(sizes[COMPANIES[0]], (4, 5, 6))  # зависит от предрасчитанных решений: у первой компании часть событий исключена

    def test_flag_false_returns_all_events(self):
        self.use_mode("cached", hide_excluded=False)
        for company in COMPANIES:
            feed = self.feed(company)
            self.assertEqual(sorted(i["event"]["event_id"] for i in feed), sorted(EVENT_IDS))
            excluded = [i for i in feed if i["relevance"]["evidence_status"] == "excluded"]
            for item in excluded:
                self.assertEqual((item["relevance"]["score"], item["relevance"]["level"]), (0.0, "low"))
            self.assertEqual(feed[-1]["relevance"]["level"], "low")

    def test_hidden_events_are_exactly_the_excluded_ones(self):
        self.use_mode("cached", hide_excluded=False)
        full = self.feed(COMPANIES[0])
        excluded_ids = {i["event"]["event_id"] for i in full if i["relevance"]["evidence_status"] == "excluded"}
        self.use_mode("cached", hide_excluded=True)
        shown = {i["event"]["event_id"] for i in self.feed(COMPANIES[0])}
        self.assertEqual({i["event"]["event_id"] for i in full} - shown, excluded_ids)

    def test_flag_does_not_change_scores_of_remaining_events(self):
        self.use_mode("cached", hide_excluded=False)
        full = {i["event"]["event_id"]: i["relevance"]["score"] for i in self.feed(COMPANIES[0])}
        self.use_mode("cached", hide_excluded=True)
        for item in self.feed(COMPANIES[0]):
            self.assertEqual(item["relevance"]["score"], full[item["event"]["event_id"]])

    def test_excluded_card_is_still_openable_directly(self):
        self.use_mode("cached", hide_excluded=True)
        j = self.analysis("e7_noise_sports", COMPANIES[0], "cfo")
        check_insight(self, j, company=COMPANIES[0], role="cfo")
        self.assertEqual(j["relevance"]["score"], 0.0)
        self.assertEqual(j["relevance"]["evidence_status"], "excluded")

    def test_template_mode_ignores_the_flag(self):
        self.use_mode("template", hide_excluded=True)
        self.assertEqual(len(self.feed(COMPANIES[0])), 7)


class TestLlmModeApi(ApiCase):
    """Режим llm на FakeLLM: те же формы, учёт токенов и ₽ в /metrics, без обращения к сети."""

    def responder(self, req: ChatRequest) -> str:
        if req.purpose == "chat":
            return json.dumps({"answer": "Ставка влияет на стоимость нового кредитования.", "evidence_ids": ["n_e1_1"], "insufficient": False}, ensure_ascii=False)
        if req.purpose == "classify":
            return json.dumps({"topic": "fx", "business_relevant": True}, ensure_ascii=False)
        return json.dumps(draft(materiality="high", urgency="medium"), ensure_ascii=False)

    def setUp(self):
        super().setUp()
        from tests.analyst.test_modes import NetworkForbidden

        guard = NetworkForbidden()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        self.use_mode("llm", hide_excluded=False, responder=self.responder, extra_env={"ANALYST_PREFER_CACHE": "false"})

    def test_feed_card_chat_and_metrics(self):
        for item in self.feed(COMPANIES[0]):
            check_feed_item(self, item)
        j = self.analysis("e1_key_rate")
        check_insight(self, j, company=COMPANIES[0], role="cfo", sum_matches=True)
        self.assertEqual(j["generated_by"], "live")
        r = self.client.post(f"{API}/chat/ask", json={"company_id": COMPANIES[0], "news_id": "e1_key_rate", "role": "cfo", "run_id": j["run_id"], "question": "Что это значит?"})
        a = r.json()
        check_answer(self, a)
        self.assertEqual(a["evidence_ids"], ["n_e1_1"])
        m = self.get("/metrics").json()
        self.assertGreater(m["llm_calls"], 0)
        self.assertGreater(m["total_tokens_consumed"], 0)
        self.assertGreater(m["spent_today_rub"], 0)
        self.assertLessEqual(m["spent_today_rub"], m["analyst_ceiling_rub"])
        self.assertIsNotNone(m["estimated_cost_per_insight_rub"])
        self.assertEqual(m["analyst"]["llm"]["llm_failures"], 0)
        self.assertEqual(m["analyst"]["live_llm_status"], "ok")

    def test_each_candidate_analyzed_once_per_role(self):
        self.feed(COMPANIES[0], "cfo")
        n = len(self.fake.requests)
        self.assertEqual(n, 7)
        self.feed(COMPANIES[0], "cfo")
        self.analysis("e1_key_rate")
        self.assertEqual(len(self.fake.requests), n)  # повторы из кэша
        self.feed(COMPANIES[0], "ceo")
        self.assertEqual(len(self.fake.requests), n + 7)

    def test_upload_classified_by_llm(self):
        event = self.client.post(f"{API}/news", json={"title": "Курс изменился", "text": "Подробности."}).json()
        self.assertEqual((event["topic"], event["topic_label"]), ("fx", "Валюта"))

    def test_high_materiality_card_may_have_estimate_only_if_high(self):
        j = self.analysis("e1_key_rate")
        if j["impact_estimate"] is not None:
            self.assertEqual(j["relevance"]["level"], "high")

    def test_metrics_do_not_leak_key(self):
        self.feed(COMPANIES[0])
        text = self.get("/metrics").text
        self.assertNotIn(TEST_KEY, text)
        self.assertNotIn("sk-", text)

    def test_as_of_is_fixed_for_reproducibility(self):
        self.assertEqual(self.svc.as_of().isoformat(), AS_OF)


if __name__ == "__main__":
    unittest.main()
