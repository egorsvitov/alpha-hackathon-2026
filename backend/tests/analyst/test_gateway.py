"""Тесты единого шлюза к LLM (без сети: httpx.MockTransport и FakeLLM, бюджет 0 ₽).

Проверяется: учёт токенов и рублей по ``usage.cost``, дневной потолок, сброс окна в 03:00 МСК,
ретраи с экспоненциальной паузой, ремонт JSON (не более двух раз), список допустимых моделей,
маскирование ключа, таймаут, потокобезопасность и режим «ключа нет».
Ключ в тестах — фиктивный (``sk-TESTKEY...``), настоящий нигде не читается.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import httpx

from app.analyst import llm_gateway as gw
from app.analyst.llm_gateway import (
    BudgetExceeded,
    BudgetLedger,
    ChatRequest,
    FakeLLM,
    FakeReply,
    GatewayStats,
    JsonRepairFailed,
    LLMError,
    LLMGateway,
    LLMUnavailable,
    MSK,
    budget_window_start,
    load_model_registry,
    mask_secrets,
)

from tests.analyst.helpers import AS_OF, bundle_event, draft, make_bundle, make_service, seed_company, seed_event

TEST_KEY = "sk-" + "TESTKEY0123456789abcdefFAKEFAKE"  # фиктивный; склейка, чтобы сканеры секретов не срабатывали
GOOD_MODEL = "z-ai/glm-5.3-flash"
BASE = "https://gateway.test/v1"
FALLBACK = "https://fallback.test/v1"
ALLOWED_MODELS = {
    "google/gemma-4-26b-a4b-it",
    "z-ai/glm-5.3-flash",
    "qwen/qwen3.6-35b-a3b",
    "deepseek/deepseek-v4.1-flash",
    "qwen/qwen3-vl-30b-a3b-instruct",
    "moonshotai/kimi-k2.6",
}


def ok_response(text: str = '{"a": 1}', *, prompt: int = 1000, completion: int = 200, cost: float | None = 0.05, reasoning: int = 0) -> httpx.Response:
    usage: dict = {"prompt_tokens": prompt, "completion_tokens": completion}
    if reasoning:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    if cost is not None:
        usage["cost"] = cost
    return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": usage})


def request(model: str = GOOD_MODEL, content: str = "привет", **kw) -> ChatRequest:
    return ChatRequest(messages=[{"role": "user", "content": content}], model=model, **kw)


class Handler:
    """Программируемый обработчик HTTP: очередь ответов/исключений + журнал запросов."""

    def __init__(self, replies: list | None = None, default=None) -> None:
        self.replies = list(replies or [])
        self.default = default
        self.calls: list[httpx.Request] = []
        self._lock = threading.Lock()

    def __call__(self, req: httpx.Request) -> httpx.Response:
        with self._lock:
            self.calls.append(req)
            reply = self.replies.pop(0) if self.replies else self.default
        if reply is None:
            return ok_response()
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(req)
        return reply


def make_gateway(handler: Handler, *, ledger: BudgetLedger | None = None, key: str = TEST_KEY, fallback: str = "", sleeps: list | None = None,
                 max_retries: int = 2, timeout_s: float = 30.0, check_key_info: bool = False, **kw) -> LLMGateway:
    sleeps = sleeps if sleeps is not None else []
    return LLMGateway(
        api_key=key, base_url=BASE, fallback_base_url=fallback, ledger=ledger or BudgetLedger(400.0),
        timeout_s=timeout_s, max_retries=max_retries, check_key_info=check_key_info,
        transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw,
    )


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def msk(y, mo, d, h, mi=0, s=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=MSK)


class TestCostAccounting(unittest.TestCase):
    def test_tokens_and_rubles_from_usage_cost(self):
        h = Handler([ok_response(prompt=1500, completion=300, cost=0.0731, reasoning=40)])
        g = make_gateway(h)
        res = g.chat(request())
        self.assertEqual((res.prompt_tokens, res.completion_tokens, res.reasoning_tokens), (1500, 300, 40))
        self.assertAlmostEqual(res.cost_rub, 0.0731)
        self.assertEqual(res.cost_source, "gateway")
        tariff = load_model_registry().get(GOOD_MODEL).tariff_cost(1500, 300)
        self.assertAlmostEqual(res.cost_rub_tariff, tariff)
        self.assertNotAlmostEqual(res.cost_rub, tariff)  # фактическая стоимость, а не тариф
        snap = g.stats.snapshot()
        self.assertEqual((snap["llm_calls"], snap["prompt_tokens"], snap["completion_tokens"], snap["reasoning_tokens"]), (1, 1500, 300, 40))
        self.assertAlmostEqual(snap["cost_rub"], 0.0731, places=4)
        self.assertEqual(snap["per_model"][GOOD_MODEL]["calls"], 1)
        self.assertEqual(snap["per_model"][GOOD_MODEL]["tokens"], 1800)
        self.assertAlmostEqual(g.ledger.spent_window_rub, 0.0731)
        self.assertAlmostEqual(g.ledger.session_spent_rub, 0.0731)
        self.assertEqual(g.ledger.reserved_rub, 0.0)  # резерв заменён фактом

    def test_tariff_used_when_gateway_reports_no_cost(self):
        g = make_gateway(Handler([ok_response(prompt=2000, completion=500, cost=None)]))
        res = g.chat(request())
        self.assertEqual(res.cost_source, "tariff")
        expected = (2000 * 5.10 + 500 * 17.0) / 1e6
        self.assertAlmostEqual(res.cost_rub, expected)
        self.assertAlmostEqual(g.ledger.spent_window_rub, expected)

    def test_negative_cost_ignored(self):
        g = make_gateway(Handler([ok_response(cost=-1)]))
        self.assertEqual(g.chat(request()).cost_source, "tariff")

    def test_costs_accumulate_across_calls(self):
        g = make_gateway(Handler(default=ok_response(cost=0.1)))
        for _ in range(4):
            g.chat(request())
        self.assertAlmostEqual(g.ledger.spent_window_rub, 0.4)
        self.assertEqual(g.stats.snapshot()["llm_calls"], 4)
        snap = g.ledger.snapshot()
        self.assertAlmostEqual(snap["spent_window_rub"], 0.4)
        self.assertAlmostEqual(snap["remaining_rub"], 399.6)
        self.assertEqual(snap["reset_at"], "03:00 MSK")

    def test_request_body_and_headers(self):
        h = Handler()
        g = make_gateway(h)
        g.chat(request(max_tokens=321, temperature=0.0))
        req = h.calls[0]
        body = json.loads(req.content)
        self.assertEqual(body["model"], GOOD_MODEL)
        self.assertEqual(body["max_tokens"], 321)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["reasoning"], {"enabled": False})  # скрытые рассуждения оплачиваются — отключены
        self.assertEqual(str(req.url), f"{BASE}/chat/completions")
        self.assertEqual(req.headers["authorization"], f"Bearer {TEST_KEY}")

    def test_chat_json_sums_usage_across_repairs(self):
        g = make_gateway(Handler([ok_response("не json", cost=0.02), ok_response('{"ok": true}', cost=0.03)]))
        data, info = g.chat_json(request(), lambda d: d)
        self.assertEqual(data, {"ok": True})
        self.assertEqual((info.calls, info.json_repairs), (2, 1))
        self.assertAlmostEqual(info.cost_rub, 0.05)

    def test_fake_llm_counts_tariff_and_ledger(self):
        ledger = BudgetLedger(400.0)
        fake = FakeLLM([FakeReply('{"a": 1}', prompt_tokens=1000, completion_tokens=100)], ledger=ledger)
        res = fake.chat(request())
        self.assertAlmostEqual(res.cost_rub, (1000 * 5.10 + 100 * 17.0) / 1e6)
        self.assertAlmostEqual(ledger.spent_window_rub, res.cost_rub)
        self.assertEqual(fake.stats.snapshot()["llm_calls"], 1)


class TestDailyCeiling(unittest.TestCase):
    def test_refusal_when_ceiling_would_be_exceeded_without_http(self):
        h = Handler()
        g = make_gateway(h, ledger=BudgetLedger(0.00001))
        with self.assertRaises(BudgetExceeded) as ctx:
            g.chat(request())
        self.assertEqual(ctx.exception.code, "budget_exhausted")
        self.assertEqual(h.calls, [])  # до сети дело не дошло
        self.assertEqual(g.ledger.reserved_rub, 0.0)
        self.assertEqual(g.ledger.spent_window_rub, 0.0)

    def test_ceiling_reached_after_spending(self):
        ledger = BudgetLedger(1.0)
        g = make_gateway(Handler(default=ok_response(cost=0.6)), ledger=ledger)
        g.chat(request())
        g.chat(request())  # проекция ещё помещается: потрачено 0,6, резерв мал
        self.assertAlmostEqual(ledger.spent_window_rub, 1.2)
        with self.assertRaises(BudgetExceeded):
            g.chat(request())
        ok, why = g.available()
        self.assertEqual((ok, why), (False, "budget_exhausted"))

    def test_ledger_reserve_and_release(self):
        ledger = BudgetLedger(10.0)
        r = ledger.reserve(4.0)
        self.assertEqual(ledger.reserved_rub, 4.0)
        with self.assertRaises(BudgetExceeded):
            ledger.reserve(6.5)  # 4 в резерве + 6,5 > 10
        ledger.release(r)
        self.assertEqual(ledger.reserved_rub, 0.0)
        ledger.reserve(6.5)

    def test_commit_replaces_reserve_with_actual(self):
        ledger = BudgetLedger(10.0)
        r = ledger.reserve(3.0)
        ledger.commit(r, 0.4)
        self.assertEqual(ledger.reserved_rub, 0.0)
        self.assertAlmostEqual(ledger.spent_window_rub, 0.4)

    def test_failed_request_releases_reserve_and_spends_nothing(self):
        g = make_gateway(Handler(default=httpx.Response(401, text="no")), ledger=BudgetLedger(400.0))
        with self.assertRaises(LLMUnavailable):
            g.chat(request())
        self.assertEqual(g.ledger.reserved_rub, 0.0)
        self.assertEqual(g.ledger.spent_window_rub, 0.0)
        self.assertEqual(g.stats.snapshot()["llm_failures"], 1)

    def test_session_cap(self):
        ledger = BudgetLedger(400.0, session_cap_rub=0.5)
        ledger.reserve(0.3)
        with self.assertRaises(BudgetExceeded) as ctx:
            ledger.reserve(0.3)
        self.assertIn("Лимит процесса", str(ctx.exception))

    def test_gateway_budget_message_from_http_402(self):
        body = httpx.Response(402, json={"error": {"message": "Budget has been exceeded for this key"}})
        h = Handler(default=body)
        g = make_gateway(h)
        with self.assertRaises(BudgetExceeded):
            g.chat(request())
        self.assertEqual(len(h.calls), 1)  # без повторов
        self.assertEqual(g.ledger.reserved_rub, 0.0)

    def test_remote_spend_from_key_info_blocks_requests(self):
        def route(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/key/info":
                return httpx.Response(200, json={"info": {"spend": 399.999, "max_budget": 500.0}})
            return ok_response()

        h = Handler(default=route)
        g = make_gateway(h, check_key_info=True)
        with self.assertRaises(BudgetExceeded):
            g.chat(request(max_tokens=1600))
        paths = [c.url.path for c in h.calls]
        self.assertEqual(paths, ["/key/info"])  # chat/completions не вызывался
        self.assertEqual(h.calls[0].headers["authorization"], f"Bearer {TEST_KEY}")
        self.assertAlmostEqual(g.ledger.team_spend_estimate(), 399.999)

    def test_remote_max_budget_lowers_ceiling(self):
        ledger = BudgetLedger(400.0)
        ledger.update_remote(10.0, max_budget=20.0)
        self.assertEqual(ledger.effective_ceiling_rub, 20.0)
        with self.assertRaises(BudgetExceeded):
            ledger.reserve(11.0)

    def test_key_info_failure_does_not_break_chat(self):
        def route(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/key/info":
                return httpx.Response(500, text="oops")
            return ok_response()

        g = make_gateway(Handler(default=route), check_key_info=True)
        self.assertEqual(g.chat(request()).text, '{"a": 1}')
        self.assertEqual(g.key_info.last_error, "HTTP 500")


class TestBudgetWindow03Msk(unittest.TestCase):
    def test_window_start_boundaries(self):
        self.assertEqual(budget_window_start(msk(2026, 10, 22, 2, 59, 59)), msk(2026, 10, 21, 3))
        self.assertEqual(budget_window_start(msk(2026, 10, 22, 3, 0, 0)), msk(2026, 10, 22, 3))
        self.assertEqual(budget_window_start(msk(2026, 10, 22, 3, 1)), msk(2026, 10, 22, 3))
        self.assertEqual(budget_window_start(msk(2026, 10, 22, 23, 59)), msk(2026, 10, 22, 3))
        self.assertEqual(budget_window_start(msk(2026, 10, 22, 0, 0)), msk(2026, 10, 21, 3))

    def test_window_start_accepts_any_timezone(self):
        # 23:59 UTC = 02:59 MSK следующих суток → окно началось в 03:00 МСК предыдущей даты
        utc = datetime(2026, 10, 21, 23, 59, tzinfo=timezone.utc)
        self.assertEqual(budget_window_start(utc), msk(2026, 10, 21, 3))
        utc2 = datetime(2026, 10, 22, 0, 1, tzinfo=timezone.utc)  # 03:01 МСК
        self.assertEqual(budget_window_start(utc2), msk(2026, 10, 22, 3))

    def test_window_start_month_and_year_rollover(self):
        self.assertEqual(budget_window_start(msk(2026, 1, 1, 2, 59)), msk(2025, 12, 31, 3))
        self.assertEqual(budget_window_start(msk(2026, 3, 1, 1, 0)), msk(2026, 2, 28, 3))

    def test_ledger_not_reset_at_0259_reset_at_0301(self):
        clock = FakeClock(msk(2026, 10, 22, 2, 50))
        ledger = BudgetLedger(5.0, clock=clock)
        ledger.commit(ledger.reserve(0.1), 4.9)
        clock.now = msk(2026, 10, 22, 2, 59)
        with self.assertRaises(BudgetExceeded):
            ledger.reserve(0.5)  # окно ещё прежнее
        self.assertAlmostEqual(ledger.snapshot()["spent_window_rub"], 4.9)
        clock.now = msk(2026, 10, 22, 3, 1)
        snap = ledger.snapshot()
        self.assertEqual(snap["spent_window_rub"], 0.0)  # новые сутки
        self.assertEqual(snap["window_start"], msk(2026, 10, 22, 3).isoformat())
        self.assertEqual(snap["next_reset_at"], msk(2026, 10, 23, 3).isoformat())
        ledger.reserve(0.5)  # теперь можно

    def test_day_rollover_resets_remote_spend(self):
        clock = FakeClock(msk(2026, 10, 22, 12))
        ledger = BudgetLedger(400.0, clock=clock)
        ledger.update_remote(300.0, max_budget=500.0)
        self.assertAlmostEqual(ledger.team_spend_estimate(), 300.0)
        clock.now = msk(2026, 10, 23, 3, 1)
        ledger.snapshot()
        self.assertEqual(ledger.team_spend_estimate(), 0.0)
        self.assertIsNone(ledger.remote_spend_rub)

    def test_state_file_survives_restart_within_window_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            clock = FakeClock(msk(2026, 10, 22, 10))
            a = BudgetLedger(100.0, state_path=path, clock=clock)
            a.commit(a.reserve(1.0), 2.5)
            same_window = BudgetLedger(100.0, state_path=path, clock=clock)
            self.assertAlmostEqual(same_window.spent_window_rub, 2.5)
            clock.now = msk(2026, 10, 23, 3, 1)
            next_window = BudgetLedger(100.0, state_path=path, clock=clock)
            self.assertEqual(next_window.spent_window_rub, 0.0)

    def test_corrupted_state_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            path.write_text("{ это не json", encoding="utf-8")
            self.assertEqual(BudgetLedger(100.0, state_path=path).spent_window_rub, 0.0)


class TestRetries(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(gw.random, "uniform", return_value=0.0)  # без случайной добавки
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retry_on_5xx_then_success_with_exponential_pause(self):
        sleeps: list[float] = []
        h = Handler([httpx.Response(503, text="busy"), httpx.Response(502, text="bad gw"), ok_response()])
        g = make_gateway(h, sleeps=sleeps, max_retries=2)
        res = g.chat(request())
        self.assertEqual(res.attempts, 3)
        self.assertEqual(len(h.calls), 3)
        self.assertEqual(sleeps, [0.8, 1.6])  # экспонента: 0,8 · 2^(n-1)
        self.assertEqual(g.stats.snapshot()["llm_retries"], 2)

    def test_pause_is_capped_and_grows(self):
        sleeps: list[float] = []
        h = Handler(default=httpx.Response(500, text="boom"))
        g = make_gateway(h, sleeps=sleeps, max_retries=6)
        with self.assertRaises(LLMUnavailable):
            g.chat(request())
        self.assertEqual(sleeps, [0.8, 1.6, 3.2, 6.4, 8.0, 8.0])
        self.assertEqual(len(h.calls), 7)

    def test_pause_has_bounded_jitter(self):
        sleeps: list[float] = []
        with mock.patch.object(gw.random, "uniform", return_value=0.3):
            g = make_gateway(Handler([httpx.Response(429, text="slow"), ok_response()]), sleeps=sleeps)
            g.chat(request())
        self.assertAlmostEqual(sleeps[0], 1.1)

    def test_gives_up_after_limit_with_unavailable(self):
        h = Handler(default=httpx.Response(503, text="busy"))
        sleeps: list[float] = []
        g = make_gateway(h, sleeps=sleeps, max_retries=2)
        with self.assertRaises(LLMUnavailable) as ctx:
            g.chat(request())
        self.assertEqual(len(h.calls), 3)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(ctx.exception.code, "unavailable")
        self.assertIn("3", str(ctx.exception))
        self.assertEqual(g.ledger.reserved_rub, 0.0)

    def test_zero_retries_means_single_attempt(self):
        h = Handler(default=httpx.Response(503, text="busy"))
        g = make_gateway(h, max_retries=0)
        with self.assertRaises(LLMUnavailable):
            g.chat(request())
        self.assertEqual(len(h.calls), 1)

    def test_no_retry_on_auth_error(self):
        for status in (401, 403):
            h = Handler(default=httpx.Response(status, text="denied"))
            g = make_gateway(h)
            with self.assertRaises(LLMUnavailable) as ctx:
                g.chat(request())
            self.assertEqual(ctx.exception.code, "auth")
            self.assertEqual(len(h.calls), 1)

    def test_no_retry_on_client_error(self):
        h = Handler(default=httpx.Response(400, text="bad request"))
        g = make_gateway(h)
        with self.assertRaises(LLMError) as ctx:
            g.chat(request())
        self.assertEqual(ctx.exception.code, "http_error")
        self.assertEqual(len(h.calls), 1)

    def test_retry_on_network_error(self):
        h = Handler([httpx.ConnectError("нет соединения"), ok_response()])
        g = make_gateway(h)
        self.assertEqual(g.chat(request()).attempts, 2)

    def test_fallback_base_url_used_on_last_attempt(self):
        h = Handler([httpx.Response(503, text="x"), httpx.Response(503, text="x"), ok_response()])
        g = make_gateway(h, fallback=FALLBACK, max_retries=2)
        g.chat(request())
        hosts = [c.url.host for c in h.calls]
        self.assertEqual(hosts, ["gateway.test", "gateway.test", "fallback.test"])

    def test_bad_response_format_is_not_retried_and_not_charged(self):
        h = Handler(default=httpx.Response(200, json={"unexpected": True}))
        g = make_gateway(h)
        with self.assertRaises(LLMError) as ctx:
            g.chat(request())
        self.assertEqual(ctx.exception.code, "bad_response")
        self.assertEqual(g.ledger.spent_window_rub, 0.0)


class TestTimeout(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(gw.random, "uniform", return_value=0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_client_uses_configured_timeout(self):
        g = make_gateway(Handler(), timeout_s=12.5)
        self.assertEqual(g._client.timeout.read, 12.5)
        self.assertEqual(g._client.timeout.connect, 10.0)
        self.assertEqual(g.timeout_s, 12.5)

    def test_timeouts_are_retried_then_reported(self):
        sleeps: list[float] = []
        h = Handler(default=httpx.ReadTimeout("read timed out"))
        g = make_gateway(h, sleeps=sleeps, max_retries=2)
        with self.assertRaises(LLMUnavailable) as ctx:
            g.chat(request())
        self.assertIn("таймаут", str(ctx.exception))
        self.assertEqual(len(h.calls), 3)
        self.assertEqual(g.ledger.spent_window_rub, 0.0)  # за таймаут ничего не списали
        self.assertEqual(g.ledger.reserved_rub, 0.0)

    def test_timeout_then_success(self):
        g = make_gateway(Handler([httpx.ConnectTimeout("t"), ok_response()]))
        self.assertEqual(g.chat(request()).attempts, 2)

    def test_service_degrades_when_gateway_times_out(self):
        h = Handler(default=httpx.ReadTimeout("t"))
        llm = make_gateway(h, max_retries=1)
        svc, _, _ = make_service(mode="llm", with_llm=False)
        svc.llm = llm
        svc.analyst.llm = llm
        calls = []

        def template():
            calls.append(1)
            from app.services.orchestrator import orchestrator

            return orchestrator.run_pipeline(event=seed_event("e1_key_rate"), company=seed_company(), role="cfo", score_fn=_score)

        ins = svc.insight(seed_company(), "cfo", seed_event("e1_key_rate"), _score, template)
        self.assertEqual(calls, [1])  # кэша нет, шлюз молчит — откат на шаблон без исключения
        self.assertTrue(any(t.tool == "llm_analyst" and t.status == "warn" for t in ins.trace))


def _score(event, company, role):
    from app.services.store import store

    return store._score_event_for_company(event, company, role)  # noqa: SLF001


class TestJsonRepair(unittest.TestCase):
    def test_repair_succeeds_on_second_and_third_attempt(self):
        fake = FakeLLM(["не json", "{ битый", '{"ok": 1}'])
        data, info = fake.chat_json(request(), lambda d: d)
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(info.json_repairs, 2)
        self.assertEqual(info.calls, 3)
        self.assertEqual(fake.stats.snapshot()["json_repairs"], 2)

    def test_at_most_two_repairs_then_failure(self):
        fake = FakeLLM(["1", "2", "3", '{"ok": 1}'])
        with self.assertRaises(JsonRepairFailed) as ctx:
            fake.chat_json(request(), lambda d: d)
        self.assertEqual(len(fake.requests), 3)  # 1 запрос + 2 ремонта, четвёртого нет
        self.assertEqual(ctx.exception.code, "invalid_json")
        self.assertEqual(ctx.exception.usage.json_repairs, 2)

    def test_limit_is_clamped_to_two_even_if_configured_higher(self):
        fake = FakeLLM(lambda r: "всегда не json", max_json_repairs=9)
        self.assertEqual(fake.max_json_repairs, 2)
        with self.assertRaises(JsonRepairFailed):
            fake.chat_json(request(), lambda d: d)
        self.assertEqual(len(fake.requests), 3)

    def test_gateway_repair_limit_over_http(self):
        h = Handler(default=ok_response("всё ещё не json"))
        g = make_gateway(h, max_json_repairs=5)
        with self.assertRaises(JsonRepairFailed):
            g.chat_json(request(), lambda d: d)
        self.assertEqual(len(h.calls), 3)

    def test_zero_repairs_configured(self):
        fake = FakeLLM(["нет"], max_json_repairs=0)
        with self.assertRaises(JsonRepairFailed):
            fake.chat_json(request(), lambda d: d)
        self.assertEqual(len(fake.requests), 1)

    def test_schema_error_also_triggers_repair(self):
        def validate(d):
            if "decision" not in d:
                raise ValueError("нет поля decision")
            return d

        fake = FakeLLM(['{"x": 1}', '{"decision": "include"}'])
        data, info = fake.chat_json(request(), validate)
        self.assertEqual(data["decision"], "include")
        self.assertEqual(info.json_repairs, 1)

    def test_repair_prompt_contains_previous_answer_and_error(self):
        fake = FakeLLM(["сломанный ответ", '{"ok": 1}'])
        fake.chat_json(request(), lambda d: d)
        second = fake.requests[1].messages
        self.assertEqual(second[-2]["role"], "assistant")
        self.assertEqual(second[-2]["content"], "сломанный ответ")
        self.assertEqual(second[-1]["role"], "user")
        self.assertEqual(second[0], fake.requests[0].messages[0])  # исходный вопрос сохранён

    def test_json_in_code_fence_and_with_prose_is_accepted_without_repair(self):
        fake = FakeLLM(['Вот ответ:\n```json\n{"ok": 1}\n```'])
        data, info = fake.chat_json(request(), lambda d: d)
        self.assertEqual((data, info.json_repairs), ({"ok": 1}, 0))

    def test_extract_json_object_errors(self):
        for bad in ("", "нет фигурных скобок", "[1, 2, 3]"):
            with self.assertRaises(Exception):
                gw.extract_json_object(bad)
        with self.assertRaises(ValueError):
            gw.extract_json_object(None)  # type: ignore[arg-type]

    def test_repaired_call_costs_are_all_booked(self):
        ledger = BudgetLedger(400.0)
        fake = FakeLLM([FakeReply("плохо", 1000, 100), FakeReply('{"a": 1}', 1200, 100)], ledger=ledger)
        _, info = fake.chat_json(request(), lambda d: d)
        self.assertAlmostEqual(ledger.spent_window_rub, info.cost_rub)
        self.assertEqual(info.calls, 2)


class TestAllowedModels(unittest.TestCase):
    def test_registry_contains_exactly_organizer_models(self):
        self.assertEqual(set(load_model_registry().models), ALLOWED_MODELS)

    def test_defaults_from_settings_are_allowed(self):
        from app.analyst.settings import DEFAULT_FALLBACK_MODEL, DEFAULT_MODEL

        reg = load_model_registry()
        self.assertEqual(reg.get(DEFAULT_MODEL).model_id, DEFAULT_MODEL)
        self.assertEqual(reg.get(DEFAULT_FALLBACK_MODEL).model_id, DEFAULT_FALLBACK_MODEL)

    def test_forbidden_model_raises_value_error(self):
        for bad in ("openai/gpt-4o", "anthropic/claude-sonnet", "", "GLM-5.3-Flash", "z-ai/glm-5.3-flash "):
            with self.assertRaises(ValueError, msg=bad):
                load_model_registry().get(bad)

    def test_gateway_rejects_forbidden_model_before_network(self):
        h = Handler()
        g = make_gateway(h)
        with self.assertRaises(ValueError) as ctx:
            g.chat(request(model="openai/gpt-4o"))
        self.assertIn("не входит в список", str(ctx.exception))
        self.assertEqual(h.calls, [])
        self.assertEqual(g.ledger.spent_window_rub, 0.0)

    def test_fake_llm_rejects_forbidden_model_too(self):
        fake = FakeLLM(lambda r: "{}")
        with self.assertRaises(ValueError):
            fake.chat(request(model="meta/llama-3"))
        self.assertEqual(fake.requests, [])

    def test_every_allowed_model_has_positive_tariff(self):
        for model in load_model_registry().models.values():
            self.assertGreater(model.tariff_cost(1000, 1000), 0)

    def test_registry_file_without_unknown_extra_models(self):
        raw = json.loads(gw.MODELS_FILE.read_text(encoding="utf-8"))
        self.assertEqual(set(raw["models"]), ALLOWED_MODELS)


class TestSecretMasking(unittest.TestCase):
    def test_pattern_masking(self):
        text = f"ключ {TEST_KEY} и заголовок Authorization: Bearer abcDEF1234567890 и api_key=\"verysecretvalue\""
        masked = mask_secrets(text)
        self.assertNotIn("TESTKEY", masked)
        self.assertNotIn("abcDEF1234567890", masked)
        self.assertNotIn("verysecretvalue", masked)
        self.assertIn("***", masked)

    def test_unregistered_sk_key_masked_by_pattern(self):
        self.assertEqual(mask_secrets("ключ sk-" + "NEVERREGISTERED123 конец"), "ключ sk-*** конец")

    def test_registered_secret_is_masked_even_without_sk_prefix(self):
        secret = "plainSecretValue-98765"
        gw.register_secret(secret)
        self.assertEqual(mask_secrets(f"утечка {secret} конец"), "утечка *** конец")

    def test_none_and_empty_are_safe(self):
        self.assertEqual(mask_secrets(""), "")
        gw.register_secret(None)
        gw.register_secret("abc")  # слишком короткий — не регистрируется, не падает

    def test_logs_are_masked(self):
        make_gateway(Handler())  # регистрирует ключ
        with self.assertLogs("app.analyst.llm", level="INFO") as cm:
            gw.logger.info("запрос с ключом %s", TEST_KEY)
            gw.logger.warning("Authorization: Bearer %s", "tokenvalue1234567")
        joined = "\n".join(cm.output)
        self.assertNotIn("TESTKEY", joined)
        self.assertNotIn("tokenvalue1234567", joined)

    def test_retry_logs_do_not_contain_key(self):
        sleeps: list = []
        h = Handler([httpx.Response(503, text=f"echo {TEST_KEY}"), ok_response()])
        with mock.patch.object(gw.random, "uniform", return_value=0.0):
            g = make_gateway(h, sleeps=sleeps)
            with self.assertLogs("app.analyst.llm", level="DEBUG") as cm:
                g.chat(request())
        self.assertNotIn("TESTKEY", "\n".join(cm.output))

    def test_exception_message_masks_key_echoed_by_server(self):
        h = Handler(default=httpx.Response(400, text=f"invalid key {TEST_KEY} for request"))
        g = make_gateway(h)
        with self.assertRaises(LLMError) as ctx:
            g.chat(request())
        self.assertNotIn("TESTKEY", str(ctx.exception))
        self.assertNotIn("TESTKEY", repr(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_llm_error_masks_message_in_constructor(self):
        self.assertNotIn("TESTKEY", str(LLMError(f"плохо: {TEST_KEY}")))
        self.assertNotIn("TESTKEY", str(LLMUnavailable(f"нет: {TEST_KEY}")))
        self.assertNotIn("TESTKEY", str(BudgetExceeded(f"бюджет: {TEST_KEY}")))
        self.assertNotIn("TESTKEY", str(JsonRepairFailed(f"json: {TEST_KEY}")))

    def test_gateway_repr_and_str_do_not_expose_key(self):
        g = make_gateway(Handler())
        for text in (repr(g), str(g), repr(g.key_info.url)):
            self.assertNotIn("TESTKEY", text)

    def test_settings_repr_hides_key(self):
        from app.analyst.settings import AnalystSettings

        s = AnalystSettings.from_env({"LLM_API_KEY": TEST_KEY, "ANALYST_MODE": "llm"})
        self.assertTrue(s.has_key)
        self.assertNotIn("TESTKEY", repr(s))
        self.assertNotIn("TESTKEY", str(s))

    def test_json_repair_errors_are_masked(self):
        fake = FakeLLM([f"плохой ответ {TEST_KEY}"] * 3)
        gw.register_secret(TEST_KEY)
        with self.assertRaises(JsonRepairFailed) as ctx:
            fake.chat_json(request(), lambda d: d)
        self.assertNotIn("TESTKEY", str(ctx.exception))
        self.assertNotIn("TESTKEY", " ".join(ctx.exception.usage.repair_errors))

    def test_key_not_in_request_body(self):
        h = Handler()
        make_gateway(h).chat(request())
        self.assertNotIn(b"TESTKEY", h.calls[0].content)

    def test_cache_does_not_persist_key(self):
        from app.analyst.cache import AnalysisCache

        with tempfile.TemporaryDirectory() as tmp:
            cache = AnalysisCache(Path(tmp), None)
            cache.put("k", {"text": f"утечка {TEST_KEY}"}, label="x/y")
            content = "".join(p.read_text(encoding="utf-8") for p in Path(tmp).rglob("*.json"))
            self.assertNotIn("TESTKEY", content)

    def test_masking_filter_attached_once(self):
        logger = gw.get_logger("app.analyst.llm")
        gw.get_logger("app.analyst.llm")
        self.assertEqual(sum(isinstance(f, gw.SecretMaskingFilter) for f in logger.filters), 1)
        self.assertIsInstance(logger, logging.Logger)


class TestThreadSafety(unittest.TestCase):
    THREADS = 16

    def run_threads(self, target) -> None:
        barrier = threading.Barrier(self.THREADS)
        errors: list[BaseException] = []

        def runner(i: int) -> None:
            try:
                barrier.wait()
                target(i)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=runner, args=(i,)) for i in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_ledger_commits_do_not_lose_updates(self):
        ledger = BudgetLedger(10_000.0)

        def work(_):
            for _ in range(200):
                ledger.commit(ledger.reserve(0.01), 0.01)

        self.run_threads(work)
        self.assertAlmostEqual(ledger.spent_window_rub, self.THREADS * 200 * 0.01, places=6)
        self.assertAlmostEqual(ledger.session_spent_rub, self.THREADS * 200 * 0.01, places=6)
        self.assertAlmostEqual(ledger.reserved_rub, 0.0, places=9)

    def test_reservations_never_exceed_ceiling(self):
        ledger = BudgetLedger(1.0)  # 0,125 точно представимо: ровно 8 резервов помещаются
        granted: list[int] = []
        lock = threading.Lock()

        def work(i):
            try:
                ledger.reserve(0.125)
            except BudgetExceeded:
                return
            with lock:
                granted.append(i)

        self.run_threads(work)
        self.assertEqual(len(granted), 8)
        self.assertLessEqual(ledger.reserved_rub, 1.0)

    def test_stats_counters_are_consistent(self):
        stats = GatewayStats()
        result = gw.ChatResult(text="x", model=GOOD_MODEL, prompt_tokens=10, completion_tokens=5, cost_rub=0.01, attempts=2)

        def work(_):
            for _ in range(300):
                stats.record(result)
                stats.record_failure()
                stats.record_repair()

        self.run_threads(work)
        snap = stats.snapshot()
        n = self.THREADS * 300
        self.assertEqual(snap["llm_calls"], n)
        self.assertEqual(snap["llm_failures"], n)
        self.assertEqual(snap["json_repairs"], n)
        self.assertEqual(snap["llm_retries"], n)
        self.assertEqual(snap["prompt_tokens"], 10 * n)
        self.assertEqual(snap["completion_tokens"], 5 * n)
        self.assertAlmostEqual(snap["cost_rub"], 0.01 * n, places=2)
        self.assertEqual(snap["per_model"][GOOD_MODEL]["calls"], n)

    def test_gateway_chat_from_many_threads(self):
        h = Handler(default=ok_response(cost=0.01))
        g = make_gateway(h)

        def work(_):
            for _ in range(10):
                g.chat(request())

        self.run_threads(work)
        n = self.THREADS * 10
        self.assertEqual(len(h.calls), n)
        self.assertEqual(g.stats.snapshot()["llm_calls"], n)
        self.assertAlmostEqual(g.ledger.spent_window_rub, 0.01 * n, places=6)
        self.assertAlmostEqual(g.ledger.reserved_rub, 0.0, places=9)

    def test_fake_llm_from_many_threads(self):
        ledger = BudgetLedger(10_000.0)
        fake = FakeLLM(lambda r: '{"ok": 1}', ledger=ledger)

        def work(_):
            for _ in range(20):
                fake.chat_json(request(), lambda d: d)

        self.run_threads(work)
        self.assertEqual(len(fake.requests), self.THREADS * 20)
        self.assertEqual(fake.stats.snapshot()["llm_calls"], self.THREADS * 20)

    def test_snapshot_during_updates_is_consistent(self):
        ledger = BudgetLedger(10_000.0)

        def work(i):
            for _ in range(100):
                if i % 2:
                    ledger.commit(ledger.reserve(0.01), 0.01)
                else:
                    snap = ledger.snapshot()
                    assert snap["reserved_rub"] >= 0

        self.run_threads(work)


class TestNoKeyMode(unittest.TestCase):
    def test_gateway_without_key_is_unavailable_not_crashing(self):
        h = Handler()
        g = make_gateway(h, key="")
        self.assertEqual(g.available(), (False, "no_key"))
        with self.assertRaises(LLMUnavailable) as ctx:
            g.chat(request())
        self.assertEqual(ctx.exception.code, "no_key")
        self.assertEqual(h.calls, [])  # даже /key/info не дёргаем
        self.assertIsNone(g.refresh_remote_spend(force=True))

    def test_llm_mode_without_key_has_no_gateway(self):
        svc, _, _ = make_service(mode="llm", with_llm=False)
        self.assertIsNone(svc.llm)
        self.assertEqual(svc.live_status(), (False, "ключ LLM_API_KEY не задан"))
        m = svc.metrics()
        self.assertFalse(m["live_llm_available"])
        self.assertEqual(m["analyst_mode"], "llm")

    def test_llm_mode_without_key_serves_bundled_cache(self):
        from app.analyst.settings import PACKAGE_DIR

        svc, _, _ = make_service(mode="llm", with_llm=False, bundled=PACKAGE_DIR / "cache")
        ins = svc.insight(seed_company("SYNTHETIC_U_1042"), "cfo", seed_event("e1_key_rate"), _score, lambda: self.fail("шаблон не нужен"))
        self.assertEqual(ins.generated_by, "cached")
        self.assertEqual(ins.status, "ok")

    def test_llm_mode_without_key_and_cache_falls_back_to_template(self):
        svc, _, _ = make_service(mode="llm", with_llm=False)  # пустой кэш
        used = []

        def template():
            used.append(True)
            from app.services.orchestrator import orchestrator

            return orchestrator.run_pipeline(event=seed_event("e1_key_rate"), company=seed_company(), role="cfo", score_fn=_score)

        ins = svc.insight(seed_company(), "cfo", seed_event("e1_key_rate"), _score, template)
        self.assertEqual(used, [True])
        self.assertEqual(svc.fallbacks_to_template, 1)
        self.assertIn("недоступен", ins.trace[-1].summary)

    def test_feed_without_key_and_cache_does_not_crash(self):
        svc, _, _ = make_service(mode="llm", with_llm=False)
        feed = svc.feed(seed_company(), "cfo", [seed_event("e1_key_rate"), seed_event("e3_rub_weak")], _score)
        self.assertEqual(len(feed), 2)  # шаблонный откат по каждому кандидату
        self.assertEqual(svc.fallbacks_to_template, 2)

    def test_chat_without_key_returns_template_answer(self):
        svc, _, _ = make_service(mode="cached", with_llm=False)
        sentinel = object()
        template_answer = mock.Mock(return_value=sentinel)

        def template_insight():
            from app.services.orchestrator import orchestrator

            return orchestrator.run_pipeline(event=seed_event("e1_key_rate"), company=seed_company(), role="cfo", score_fn=_score)

        answer = svc.ask(seed_company(), "cfo", seed_event("e1_key_rate"), "Что делать?", _score, template_insight, template_answer)
        self.assertIs(answer, sentinel)
        template_answer.assert_called_once()

    def test_budget_exhausted_degrades_to_cache_or_template(self):
        # потолок 0: живой вызов невозможен, но сервис отвечает без исключений
        fake = FakeLLM(lambda r: json.dumps(draft()), ledger=BudgetLedger(0.0))
        svc, _, _ = make_service(mode="llm", with_llm=False)
        svc.llm = fake
        svc.analyst.llm = fake
        used = []

        def template():
            used.append(1)
            from app.services.orchestrator import orchestrator

            return orchestrator.run_pipeline(event=seed_event("e1_key_rate"), company=seed_company(), role="cfo", score_fn=_score)

        svc.insight(seed_company(), "cfo", seed_event("e1_key_rate"), _score, template)
        self.assertEqual(used, [1])
        self.assertEqual(fake.stats.snapshot()["llm_calls"], 0)

    def test_cached_mode_never_builds_gateway_even_with_key(self):
        svc, _, _ = make_service(mode="cached", with_llm=False, extra_env={"LLM_API_KEY": TEST_KEY})
        self.assertIsNone(svc.llm)
        self.assertEqual(svc.live_status(), (False, "режим cached"))

    def test_template_mode_service_is_disabled(self):
        svc, _, _ = make_service(mode="template", with_llm=False)
        self.assertFalse(svc.enabled)


if __name__ == "__main__":
    unittest.main()
