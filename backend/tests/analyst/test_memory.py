"""Память фактов и «что изменилось по сравнению с прошлым» (memory.py) — на FakeLLM, без сети."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.analyst import prompts
from app.analyst.analyst import BusinessAnalyst
from app.analyst.cache import AnalysisCache
from app.analyst.llm_gateway import ChatRequest, FakeLLM
from app.analyst.memory import (
    CONTEXT_TOKEN_BUDGET,
    MemoryFact,
    MemoryItem,
    MemoryStore,
    PreviousContext,
    schedule_levels,
    signature,
)
from app.analyst.schemas import LLMDraft
from app.analyst.view import build_prompt_view
from app.analyst.qa import validate_draft
from app.analyst.bridge import to_insight

from tests.analyst.helpers import AS_OF, analyst_input, bundle_event, draft, make_bundle, seed_company

V11 = prompts.PROMPT_VERSION_V11

RATE_HOLD = ("ev_rate_hold", "Банк России сохранил ключевую ставку", "Банк России сохранил ключевую ставку на уровне 17% годовых.", "2026-10-05T13:30:00+03:00")
RATE_HOLD2 = ("ev_rate_hold2", "Банк России вновь сохранил ключевую ставку", "Банк России вновь сохранил ключевую ставку на уровне 17% годовых.", "2026-10-12T13:30:00+03:00")
RATE_HIKE = ("ev_rate_hike", "Банк России повысил ключевую ставку", "Банк России повысил ключевую ставку до 18% годовых.", "2026-10-20T13:30:00+03:00")
WHEAT = ("ev_wheat", "Пошлина на экспорт пшеницы снижена", "Правительство снизило экспортную пошлину на пшеницу.", "2026-10-15T10:00:00+03:00")


def event(spec: tuple[str, str, str, str]) -> dict:
    eid, title, text, published = spec
    return bundle_event(make_bundle(eid, title, [{"text": text}], published=published))


def view_of(req: ChatRequest) -> dict:
    content = req.messages[-1]["content"]
    start = content.index("Данные (JSON):\n") + len("Данные (JSON):\n")
    end = content.rindex("\n\nВерни только JSON")
    return json.loads(content[start:end])


def memory_responder(req: ChatRequest) -> str:
    """Отвечает как добросовестная модель: сравнивает с P-записями из запроса."""
    v = view_of(req)
    prev = v.get("previous_context")
    items = prev.get("items", []) if isinstance(prev, dict) else []
    text = v["event"]["claims"][0]["text"]
    if not items:
        changes = {"status": "new", "previous_insight_id": None, "summary": None, "claim_ids": ["C1"]}
    elif "повысил" in text:
        changes = {"status": "escalation", "previous_insight_id": "P1",
                   "summary": "Ставка повышена до 18% после сохранения на уровне 17%.", "claim_ids": ["C1", "P1.1"]}
    else:
        changes = {"status": "continuation", "previous_insight_id": "P1",
                   "summary": "Ставка снова сохранена на уровне 17%.", "claim_ids": ["C1", "P1.1"]}
    return json.dumps(draft(changes_since_previous=changes), ensure_ascii=False)


def make_analyst(memory: MemoryStore | None, responder=memory_responder, cache_dir: Path | None = None):
    fake = FakeLLM(responder)
    cache = AnalysisCache(cache_dir, None) if cache_dir else None
    analyst = BusinessAnalyst(fake, model="z-ai/glm-5.3-flash", cache=cache, max_parallel=4,
                              prompt_version=V11, memory=memory)
    return analyst, fake


def changes_by_event(output) -> dict[str, dict]:
    return {i.event_refs[0].event_id: (i.changes_since_previous.model_dump() if i.changes_since_previous else None)
            for i in output.ranked_insights}


class TestMemoryChain(unittest.TestCase):
    def setUp(self) -> None:
        self.company = seed_company()
        self.mem = MemoryStore(":memory:")

    def analyze(self, analyst, specs, role="cfo", company=None):
        inp = analyst_input(company or self.company, role, [event(s) for s in specs])
        out, _ = analyst.analyze(inp)
        return out

    def test_hold_then_hold_then_hike_gives_continuation_then_escalation(self):
        analyst, fake = make_analyst(self.mem)
        c1 = changes_by_event(self.analyze(analyst, [RATE_HOLD]))
        self.assertEqual(c1["ev_rate_hold"]["status"], "new")
        self.assertIsNone(c1["ev_rate_hold"]["previous_insight_id"])

        c2 = changes_by_event(self.analyze(analyst, [RATE_HOLD2]))
        self.assertEqual(c2["ev_rate_hold2"]["status"], "continuation")
        self.assertEqual(c2["ev_rate_hold2"]["previous_insight_id"], "mem:SYNTHETIC_U_1042:cfo:ev_rate_hold@v1")
        self.assertIn("ev_rate_hold:c1", c2["ev_rate_hold2"]["claim_ids"])  # исходный ID сохранённого факта
        self.assertIn("ev_rate_hold2:c1", c2["ev_rate_hold2"]["claim_ids"])

        c3 = changes_by_event(self.analyze(analyst, [RATE_HIKE]))
        ch = c3["ev_rate_hike"]
        self.assertEqual(ch["status"], "escalation")
        self.assertTrue(ch["previous_insight_id"].startswith("mem:SYNTHETIC_U_1042:cfo:ev_rate_hold"))
        self.assertIn("18%", ch["summary"])
        # В промпт попали только записи о событиях до текущего, с ID и дословным текстом.
        prev = view_of(fake.requests[-1])["previous_context"]["items"]
        self.assertEqual({p["title"] for p in prev}, {RATE_HOLD[1], RATE_HOLD2[1]})
        self.assertEqual(prev[0]["facts"][0]["id"], "P1.1")

    def test_other_company_and_role_do_not_leak(self):
        analyst, _ = make_analyst(self.mem)
        self.analyze(analyst, [RATE_HOLD])
        other_role = changes_by_event(self.analyze(analyst, [RATE_HIKE], role="treasurer"))
        self.assertEqual(other_role["ev_rate_hike"]["status"], "new")
        other_company = changes_by_event(self.analyze(analyst, [RATE_HIKE], company=seed_company("SYNTHETIC_U_2218")))
        self.assertEqual(other_company["ev_rate_hike"]["status"], "new")

    def test_fabricated_history_is_rejected_by_qa(self):
        def liar(req: ChatRequest) -> str:
            return json.dumps(draft(changes_since_previous={
                "status": "escalation", "previous_insight_id": "P7", "summary": "Ставка выросла с 15% до 18%.", "claim_ids": ["P7.1"]}),
                ensure_ascii=False)

        analyst, _ = make_analyst(self.mem, responder=liar)
        out = self.analyze(analyst, [RATE_HIKE])  # памяти ещё нет
        ins = out.ranked_insights[0]
        self.assertEqual(ins.changes_since_previous.status, "new")
        self.assertIsNone(ins.changes_since_previous.previous_insight_id)
        codes = {i.code for r in out.run_metadata.candidates for i in r.qa_issues}
        self.assertIn("changes_without_history", codes)

    def test_unknown_previous_ref_and_number_not_in_data(self):
        self.analyze(make_analyst(self.mem)[0], [RATE_HOLD])

        def liar(req: ChatRequest) -> str:
            return json.dumps(draft(changes_since_previous={
                "status": "escalation", "previous_insight_id": "P5", "summary": "Ставка выросла с 15% до 18%.",
                "claim_ids": ["C1", "P9.9"]}), ensure_ascii=False)

        analyst, _ = make_analyst(self.mem, responder=liar)
        out = self.analyze(analyst, [RATE_HIKE])
        ch = out.ranked_insights[0].changes_since_previous
        self.assertEqual(ch.status, "unknown")
        self.assertIsNone(ch.previous_insight_id)
        self.assertIsNone(ch.summary)  # 15 нет ни в текущих, ни в сохранённых фактах
        self.assertEqual(ch.claim_ids, ["ev_rate_hike:c1"])
        codes = {i.code for r in out.run_metadata.candidates for i in r.qa_issues}
        self.assertTrue({"unknown_previous_ref", "changes_unsupported", "unknown_ref"} <= codes, codes)

    def test_without_memory_status_is_unknown(self):
        analyst, fake = make_analyst(None)
        out = self.analyze(analyst, [RATE_HIKE])
        self.assertEqual(out.ranked_insights[0].changes_since_previous.status, "unknown")
        self.assertEqual(view_of(fake.requests[0])["previous_context"], "Память выводов не подключена: история неизвестна.")

    def test_v10_prompt_has_no_previous_block_and_no_changes(self):
        fake = FakeLLM(lambda req: json.dumps(draft(), ensure_ascii=False))
        analyst = BusinessAnalyst(fake, model="z-ai/glm-5.3-flash", prompt_version=prompts.PROMPT_VERSION, memory=self.mem)
        out, _ = analyst.analyze(analyst_input(self.company, "cfo", [event(RATE_HIKE)]))
        self.assertIsNone(out.ranked_insights[0].changes_since_previous)
        self.assertNotIn("previous_context", fake.requests[0].messages[-1]["content"])


class TestTimeAndIdempotency(unittest.TestCase):
    def setUp(self) -> None:
        self.mem = MemoryStore(":memory:")

    @staticmethod
    def item(event_id: str, published: str, text: str = "Банк России сохранил ключевую ставку на уровне 17% годовых.") -> MemoryItem:
        from app.analyst.memory import stems

        return MemoryItem(company_id="C", role="cfo", event_id=event_id, event_version=1, published_at=published,
                          topic="rates", title="Ключевая ставка", decision="watch", summary="Вывод",
                          facts=(MemoryFact(claim_id=f"{event_id}:c1", text=text),), stems=frozenset(stems(text)))

    def test_events_after_as_of_are_ignored(self):
        self.mem.record(self.item("old", "2026-10-01T10:00:00+03:00"))
        self.mem.record(self.item("future", "2026-10-21T10:00:00+03:00"))
        cand = analyst_input(seed_company(), "cfo", [event(RATE_HIKE)]).candidates[0]
        sig = signature(cand)  # текущее событие 2026-10-20
        found = [it.event_id for _, it in self.mem.candidates_for("C", "cfo", sig, datetime.fromisoformat("2026-10-20T14:00:00+03:00"))]
        self.assertEqual(found, ["old"])  # «future» позже и as_of, и текущего события
        found = [it.event_id for _, it in self.mem.candidates_for("C", "cfo", sig, datetime.fromisoformat("2026-09-30T00:00:00+03:00"))]
        self.assertEqual(found, [])  # всё сохранённое — после as_of

    def test_event_after_as_of_is_not_analyzed_or_stored(self):
        analyst, fake = make_analyst(self.mem)
        late = ("ev_late", "Банк России повысил ключевую ставку", "Банк России повысил ключевую ставку до 19% годовых.", "2026-10-25T13:30:00+03:00")
        out, _ = analyst.analyze(analyst_input(seed_company(), "cfo", [event(late)]))
        self.assertEqual(out.excluded_candidates[0].reason_code, "after_as_of")
        self.assertEqual((len(fake.requests), self.mem.count()), (0, 0))

    def test_record_is_idempotent_and_newer_version_wins(self):
        it = self.item("a", "2026-10-01T10:00:00+03:00")
        self.assertTrue(self.mem.record(it))
        self.assertFalse(self.mem.record(it))
        self.assertEqual(self.mem.count(), 1)
        newer = MemoryItem(**{**it.__dict__, "event_version": 2, "summary": "Новый вывод"})
        older = MemoryItem(**{**it.__dict__, "event_version": 1, "summary": "Старый вывод"})
        self.assertTrue(self.mem.record(newer))
        self.assertFalse(self.mem.record(older))
        self.assertEqual(self.mem.items("C", "cfo")[0].summary, "Новый вывод")

    def test_rerun_hits_cache_and_keeps_memory(self):
        tmp = Path(tempfile.mkdtemp(prefix="mem_cache_"))
        analyst, fake = make_analyst(self.mem, cache_dir=tmp)
        inp = analyst_input(seed_company(), "cfo", [event(RATE_HOLD), event(RATE_HIKE), event(WHEAT)])
        out1, _ = analyst.analyze(inp)
        calls, count = len(fake.requests), self.mem.count()
        out2, _ = analyst.analyze(inp)
        self.assertEqual(len(fake.requests), calls)  # повторный запуск — только кэш
        self.assertEqual(self.mem.count(), count)
        self.assertEqual(changes_by_event(out1), changes_by_event(out2))
        self.assertEqual(changes_by_event(out1)["ev_rate_hike"]["status"], "escalation")
        self.assertEqual(changes_by_event(out1)["ev_wheat"]["status"], "new")


class TestDeterminism(unittest.TestCase):
    def test_input_order_does_not_change_result(self):
        specs = [RATE_HOLD, RATE_HOLD2, RATE_HIKE, WHEAT]
        results = []
        prompts_seen = []
        for order in (specs, list(reversed(specs)), [WHEAT, RATE_HIKE, RATE_HOLD, RATE_HOLD2]):
            mem = MemoryStore(":memory:")
            analyst, fake = make_analyst(mem)
            out, _ = analyst.analyze(analyst_input(seed_company(), "cfo", [event(s) for s in order]))
            results.append(changes_by_event(out))
            prompts_seen.append(sorted(r.messages[-1]["content"] for r in fake.requests))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])
        self.assertEqual(prompts_seen[0], prompts_seen[1])
        self.assertEqual(results[0]["ev_rate_hike"]["status"], "escalation")
        self.assertEqual(results[0]["ev_rate_hold2"]["status"], "continuation")

    def test_schedule_levels_chain_and_parallel(self):
        cands = analyst_input(seed_company(), "cfo", [event(s) for s in (RATE_HIKE, WHEAT, RATE_HOLD)]).candidates
        levels = [[signature(c).event_id for c in lv] for lv in schedule_levels(cands)]
        self.assertEqual(levels, [["ev_rate_hold", "ev_wheat"], ["ev_rate_hike"]])


class TestPreviousContextBudget(unittest.TestCase):
    def test_block_fits_budget_and_never_cuts_quotes(self):
        long_text = "Банк России сохранил ключевую ставку на уровне 17% годовых. " * 12
        items = [
            MemoryItem(company_id="C", role="cfo", event_id=f"e{i}", event_version=1, published_at=f"2026-10-0{i}T10:00:00+03:00",
                       topic="rates", title=f"Ставка {i}", decision="watch", summary="Вывод " * 20,
                       facts=tuple(MemoryFact(claim_id=f"e{i}:c{j}", text=long_text) for j in range(1, 4)))
            for i in range(1, 6)
        ]
        ctx = PreviousContext.build(items)
        block = ctx.block()
        size = len(json.dumps(block, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) // 4
        self.assertLessEqual(size, CONTEXT_TOKEN_BUDGET)
        self.assertTrue(block)
        for it in block:
            for f in it["facts"]:
                self.assertEqual(f["text"], long_text)  # цитата целиком
        self.assertTrue(ctx.ref().startswith("mem1:"))

    def test_qa_accepts_only_passed_ids(self):
        mem = MemoryStore(":memory:")
        analyst, _ = make_analyst(mem)
        analyst.analyze(analyst_input(seed_company(), "cfo", [event(RATE_HOLD)]))
        inp = analyst_input(seed_company(), "cfo", [event(RATE_HIKE)])
        cand = inp.candidates[0]
        prev = analyst.previous_context(inp, cand)
        view = build_prompt_view(inp, cand, previous=prev)
        d = LLMDraft.model_validate(view.realias_draft(draft(changes_since_previous={
            "status": "escalation", "previous_insight_id": "P1", "summary": "Ставка повышена до 18%.", "claim_ids": ["C1", "P1.1"]})))
        qa = validate_draft(d, view, "cfo", as_of=AS_OF)
        self.assertEqual(qa.changes.previous_insight_id, "mem:SYNTHETIC_U_1042:cfo:ev_rate_hold@v1")
        self.assertEqual(qa.changes.claim_ids, ["ev_rate_hike:c1", "ev_rate_hold:c1"])


class TestBridgeAndApiShape(unittest.TestCase):
    def test_insight_carries_changes_and_json_omits_empty(self):
        mem = MemoryStore(":memory:")
        analyst, _ = make_analyst(mem)
        analyst.analyze(analyst_input(seed_company(), "cfo", [event(RATE_HOLD)]))
        inp = analyst_input(seed_company(), "cfo", [event(RATE_HIKE)])
        outcome = analyst.analyze_candidate(inp, inp.candidates[0])
        ins = to_insight(outcome, event=event(RATE_HIKE), cand=inp.candidates[0], ctx=inp.company_context,
                         company_id="SYNTHETIC_U_1042", role="cfo", run_id="an_test")
        self.assertEqual(ins.changes_since_previous.status, "escalation")
        self.assertIn("changes_since_previous", json.loads(ins.model_dump_json()))
        ins.changes_since_previous = None
        self.assertNotIn("changes_since_previous", json.loads(ins.model_dump_json()))
        self.assertIn("changes_since_previous", ins.model_dump())


class TestSqliteFile(unittest.TestCase):
    def test_file_store_persists_and_is_thread_safe(self):
        import threading

        path = Path(tempfile.mkdtemp(prefix="mem_db_")) / "m.sqlite3"
        store = MemoryStore(path)

        def put(i: int) -> None:
            store.record(TestTimeAndIdempotency.item(f"t{i}", f"2026-10-0{1 + i % 9}T10:00:00+03:00"))

        threads = [threading.Thread(target=put, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        store.close()
        self.assertEqual(MemoryStore(path).count(), 20)


if __name__ == "__main__":
    unittest.main()
