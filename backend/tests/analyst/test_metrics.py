"""Тесты скрипта метрик ``app.analyst.eval.metrics`` (по кэшу, без сети и без расхода бюджета)."""

from __future__ import annotations

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from app.analyst.eval import metrics as mt
from app.analyst.llm_gateway import FakeReply
from app.analyst.settings import PACKAGE_DIR

from tests.analyst.helpers import draft, exclude_draft, make_service, seed_company, seed_event
from tests.analyst.test_gateway import TEST_KEY
from tests.analyst.test_modes import NetworkForbidden, score_fn

BUNDLED = PACKAGE_DIR / "cache"


def fake_envelope(*, cost: float, latency: int, repairs: int = 0, decision: str = "include", model: str = "m1", key: str = "k",
                  qa_status: str = "passed", issues: list[dict] | None = None, statements_with_refs: int = 2, statements_without: int = 0) -> dict:
    """Минимальная запись кэша с известными числами (для точной арифметики)."""
    stmts = [{"text": f"утверждение {i}", "kind": "inference", "claim_ids": ["c"]} for i in range(statements_with_refs)]
    stmts += [{"text": f"без ссылок {i}", "kind": "inference"} for i in range(statements_without)]
    d = draft(decision=decision)
    record = {
        "insight": {"decision": decision, "significance": "medium", "facts": [], "risks": stmts, "opportunities": [], "next_steps": [],
                    "bank_assessment": {"status": "not_found", "bank_chunk_ids": []}} if decision != "exclude" else None,
        "excluded": {"reason_code": "not_relevant"} if decision == "exclude" else None,
        "run": {"model": model, "prompt_version": "analyst-v1.0", "qa_status": qa_status, "qa_issues": issues or [],
                "usage": {"calls": 1, "prompt_tokens": 1000, "completion_tokens": 200, "reasoning_tokens": 0, "cost_rub": cost,
                          "cost_rub_tariff": cost / 2, "latency_ms": latency, "json_repairs": repairs},
                "draft": d},
    }
    return {"key": key, "model": model, "created_at": "2026-10-22T12:00:00+03:00", "record": record}


class TestComputeMetricsArithmetic(unittest.TestCase):
    def setUp(self):
        self.envs = [
            fake_envelope(cost=0.10, latency=1000, key="a", decision="include"),
            fake_envelope(cost=0.30, latency=3000, repairs=1, key="b", decision="watch", qa_status="passed_with_fixes",
                          issues=[{"code": "number_not_in_data", "severity": "error"}, {"code": "duplicate_statement", "severity": "info"}],
                          statements_with_refs=1, statements_without=1),
            fake_envelope(cost=0.20, latency=2000, key="c", decision="exclude"),
            fake_envelope(cost=0.40, latency=10000, key="d", decision="include", model="m2",
                          issues=[{"code": "unknown_ref", "severity": "warn"}], qa_status="passed_with_fixes"),
        ]
        self.m = mt.compute_metrics(self.envs, recompute_qa=False)

    def test_counts_and_models(self):
        self.assertEqual(self.m["candidates"], 4)
        self.assertEqual(self.m["models"], {"m1": 3, "m2": 1})
        self.assertEqual(self.m["prompt_versions"], {"analyst-v1.0": 4})

    def test_json_shares(self):
        self.assertEqual(self.m["json"]["valid_pct"], 100.0)
        self.assertEqual(self.m["json"]["first_try_pct"], 75.0)
        self.assertEqual(self.m["json"]["repairs_total"], 1)
        self.assertEqual(self.m["json"]["invalid"], 0)

    def test_invalid_draft_is_counted(self):
        broken = fake_envelope(cost=0.1, latency=1, key="x")
        broken["record"]["run"]["draft"] = {"decision": "банкет"}
        m = mt.compute_metrics([broken, fake_envelope(cost=0.1, latency=1, key="y")], recompute_qa=False)
        self.assertEqual((m["json"]["valid_pct"], m["json"]["invalid"]), (50.0, 1))

    def test_decisions_distribution(self):
        self.assertEqual(self.m["decisions"]["model"], {"include": 2, "watch": 1, "exclude": 1})
        self.assertEqual(self.m["decisions"]["final"], {"include": 2, "watch": 1, "exclude": 1})
        self.assertEqual(self.m["decisions"]["model_share_pct"], {"include": 50.0, "watch": 25.0, "exclude": 25.0})
        self.assertEqual(self.m["significance"], {"high": 0, "medium": 3, "low": 0, "excluded": 1})

    def test_reference_coverage(self):
        cov = self.m["reference_coverage"]
        # утверждения карточек: 2 + 2 + 2 (exclude без карточки не считается) = 6; без ссылок — 1
        self.assertEqual((cov["statements"], cov["with_refs"]), (6, 5))
        self.assertEqual(cov["pct"], 83.3)
        self.assertEqual(cov["unknown_refs_removed"], 1)

    def test_tokens(self):
        t = self.m["tokens"]
        self.assertEqual((t["prompt"], t["completion"], t["total"], t["per_candidate"]), (4000, 800, 4800, 1200))

    def test_latency(self):
        lat = self.m["latency_ms"]
        self.assertEqual((lat["avg"], lat["median"], lat["max"]), (4000, 2500, 10000))
        self.assertGreaterEqual(lat["p95"], 3000)

    def test_cost_per_candidate(self):
        c = self.m["cost_rub"]
        self.assertAlmostEqual(c["total"], 1.0)
        self.assertAlmostEqual(c["per_candidate"], 0.25)
        self.assertAlmostEqual(c["tariff_total"], 0.5)
        self.assertAlmostEqual(c["tariff_per_candidate"], 0.125)

    def test_qa_counts_from_stored(self):
        qa = self.m["qa"]
        self.assertEqual(qa["source"], "stored")
        self.assertEqual((qa["passed"], qa["passed_with_fixes"], qa["failed"]), (2, 2, 0))
        self.assertEqual(qa["candidates_with_issues"], 2)  # info-замечания не считаются срабатываниями
        self.assertEqual(qa["issues_total"], 2)
        self.assertEqual(qa["by_code"], {"number_not_in_data": 1, "unknown_ref": 1})
        self.assertEqual(qa["by_severity"], {"error": 1, "warn": 1})
        self.assertEqual(qa["triggered_pct"], 50.0)

    def test_per_model_breakdown(self):
        pm = self.m["per_model"]
        self.assertEqual(set(pm), {"m1", "m2"})
        self.assertAlmostEqual(pm["m1"]["cost_rub_per_candidate"], 0.2)
        self.assertEqual(pm["m2"]["latency_ms_avg"], 10000)
        self.assertEqual(pm["m1"]["json_first_try_pct"], 66.7)

    def test_empty_input(self):
        m = mt.compute_metrics([])
        self.assertEqual(m["candidates"], 0)
        self.assertIsNone(m["json"]["valid_pct"])
        self.assertIsNone(m["cost_rub"]["per_candidate"])
        self.assertIsNone(m["latency_ms"]["avg"])
        self.assertIsNone(m["tokens"]["per_candidate"])
        self.assertEqual(m["decisions"]["final"], {"include": 0, "watch": 0, "exclude": 0})
        self.assertIn("Кандидатов в кэше", mt.format_table(m, "x"))  # таблица строится и для пустого кэша

    def test_record_without_usage_does_not_break_averages(self):
        envs = list(self.envs)
        bare = fake_envelope(cost=0, latency=0, key="z")
        bare["record"]["run"]["usage"] = {}
        m = mt.compute_metrics(envs + [bare], recompute_qa=False)
        self.assertAlmostEqual(m["cost_rub"]["per_candidate"], 0.25)  # усредняется по записям с вызовом модели
        self.assertEqual(m["candidates"], 5)

    def test_quantile_helper(self):
        self.assertIsNone(mt._quantile([], 0.95))
        self.assertEqual(mt._quantile([5.0], 0.95), 5.0)
        self.assertEqual(mt._quantile([1, 2, 3, 4, 100], 0.95), 100)
        self.assertEqual(mt._pct(1, 0), None)


class TestWithPipelineCache(unittest.TestCase):
    """Кэш создаётся настоящим конвейером на FakeLLM, затем читается скриптом метрик."""

    def setUp(self):
        replies = [
            FakeReply("сначала не json", prompt_tokens=900, completion_tokens=10),
            FakeReply(json.dumps(draft(materiality="high"), ensure_ascii=False), prompt_tokens=1000, completion_tokens=200),
            FakeReply(json.dumps(exclude_draft(), ensure_ascii=False), prompt_tokens=800, completion_tokens=50),
            FakeReply(json.dumps(draft(decision="watch", risks=[{"text": "Издержки вырастут на 83%.", "kind": "inference", "feature_ids": ["F1"], "claim_ids": ["C1"]}]), ensure_ascii=False),
                      prompt_tokens=1100, completion_tokens=220),
        ]
        self.tmp = Path(tempfile.mkdtemp(prefix="analyst_metrics_"))
        svc, self.fake, _ = make_service(mode="llm", responder=replies, tmp=self.tmp)
        svc.feed(seed_company("SYNTHETIC_U_1042"), "cfo", [seed_event("e1_key_rate"), seed_event("e7_noise_sports"), seed_event("e3_rub_weak")], score_fn)
        self.runtime = self.tmp / "runtime"

    def test_reads_runtime_cache(self):
        m = mt.build_report(self.runtime, recompute_qa=False)
        self.assertEqual(m["candidates"], 3)
        self.assertEqual(m["json"]["valid_pct"], 100.0)
        self.assertEqual(m["json"]["repairs_total"], 1)
        self.assertEqual(m["json"]["first_try_pct"], 66.7)
        self.assertEqual(m["decisions"]["model"]["exclude"], 1)
        self.assertEqual(m["decisions"]["model"]["include"] + m["decisions"]["model"]["watch"], 2)
        self.assertEqual(m["tokens"]["prompt"], 900 + 1000 + 800 + 1100)
        self.assertGreater(m["cost_rub"]["total"], 0)
        self.assertEqual(m["qa"]["by_code"].get("number_not_in_data"), 1)
        self.assertGreaterEqual(m["qa"]["candidates_with_issues"], 1)

    def test_recompute_matches_stored_qa_for_fresh_cache(self):
        stored = mt.build_report(self.runtime, recompute_qa=False)["qa"]
        fresh = mt.build_report(self.runtime, recompute_qa=True)["qa"]
        self.assertEqual(fresh["source"], "recomputed")
        self.assertEqual(fresh["stale"], 0)
        self.assertEqual(fresh["by_code"], stored["by_code"])
        self.assertEqual((fresh["passed"], fresh["passed_with_fixes"], fresh["failed"]), (stored["passed"], stored["passed_with_fixes"], stored["failed"]))

    def test_stale_candidate_falls_back_to_stored_qa(self):
        envs = mt.load_envelopes(self.runtime)
        for env in envs:
            env["key"] = re.sub(r"#[0-9a-f]{12}", "#000000000000", env["key"])  # данные события изменились
        m = mt.compute_metrics(envs, recompute_qa=True)
        self.assertEqual(m["qa"]["stale"], 3)
        self.assertEqual(m["qa"]["by_code"].get("number_not_in_data"), 1)  # взято сохранённое


class TestBundledCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with NetworkForbidden():
            cls.report = mt.build_report(BUNDLED)

    def test_repository_cache_is_fully_covered(self):
        r = self.report
        self.assertEqual(r["candidates"], 84)  # 3 компании x 7 событий x 4 роли
        self.assertEqual(r["json"]["valid_pct"], 100.0)
        self.assertEqual(r["reference_coverage"]["pct"], 100.0)
        self.assertEqual(r["qa"]["stale"], 0)
        self.assertEqual(sum(r["decisions"]["final"].values()), 84)
        self.assertEqual(sum(r["significance"].values()), 84)

    def test_budget_figures_are_reasonable(self):
        r = self.report
        self.assertLess(r["cost_rub"]["per_candidate"], 0.5)
        self.assertLess(r["cost_rub"]["total"], 15.0)  # весь предрасчёт укладывается в выделенные ₽
        self.assertGreater(r["tokens"]["per_candidate"], 1000)
        self.assertGreater(r["latency_ms"]["avg"], 0)

    def test_report_has_no_secrets_or_absolute_paths(self):
        text = json.dumps(self.report, ensure_ascii=False)
        self.assertNotIn("sk-", text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("\\\\", text)
        self.assertNotIn("Users", text)
        self.assertEqual(self.report["source"], "app/analyst/cache")

    def test_only_allowed_models_in_cache(self):
        from tests.analyst.test_gateway import ALLOWED_MODELS

        self.assertTrue(set(self.report["models"]) <= ALLOWED_MODELS)

    def test_report_is_deterministic(self):
        self.assertEqual(mt.build_report(BUNDLED), self.report)

    def test_committed_json_has_current_structure(self):
        path = mt.DEFAULT_OUT
        self.assertTrue(path.is_file(), "eval/metrics_latest.json должен лежать в репозитории (python -m app.analyst.eval.metrics)")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(saved), set(self.report))
        self.assertNotIn("sk-", path.read_text(encoding="utf-8"))
        self.assertEqual(saved["qa"].keys(), self.report["qa"].keys())


class TestCliAndFiles(unittest.TestCase):
    def run_main(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mt.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_table_output_and_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "m.json"
            code, out, _ = self.run_main("--out", str(target))
            self.assertEqual(code, 0)
            for label in ("Валидный JSON", "Покрытие ссылками", "Решения модели include/watch/exclude", "Токены", "Стоимость на кандидата",
                          "QA:", "Срабатывания QA по кодам"):
                self.assertIn(label, out)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["candidates"], 84)
            self.assertNotIn("sk-", target.read_text(encoding="utf-8"))
            self.assertIn("JSON сохранён", out)

    def test_json_flag_prints_parseable_json(self):
        code, out, _ = self.run_main("--json", "--no-write")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["candidates"], 84)

    def test_no_write_leaves_default_file_untouched(self):
        before = mt.DEFAULT_OUT.read_bytes() if mt.DEFAULT_OUT.exists() else None
        self.run_main("--no-write")
        after = mt.DEFAULT_OUT.read_bytes() if mt.DEFAULT_OUT.exists() else None
        self.assertEqual(before, after)

    def test_missing_dir_is_error_code(self):
        code, _, err = self.run_main("--cache-dir", str(Path(tempfile.gettempdir()) / "нет_такого_каталога_метрик"), "--no-write")
        self.assertEqual(code, 2)
        self.assertIn("не найден", err)

    def test_empty_dir_reports_empty_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self.run_main("--cache-dir", tmp, "--no-write")
            self.assertEqual(code, 0)
            self.assertIn("Кэш пуст", out)

    def test_no_recompute_flag(self):
        code, out, _ = self.run_main("--no-recompute", "--no-write")
        self.assertEqual(code, 0)
        self.assertIn("stored", out)

    def test_corrupted_and_foreign_files_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.json").write_text("{ не json", encoding="utf-8")
            (root / "foreign.json").write_text(json.dumps({"hello": "world"}), encoding="utf-8")
            (root / "ok.json").write_text(json.dumps(fake_envelope(cost=0.1, latency=100)), encoding="utf-8")
            self.assertEqual(len(mt.load_envelopes(root)), 1)
            self.assertEqual(mt.load_envelopes(root / "нет"), [])

    def test_write_report_masks_secrets_and_uses_lf(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "r.json"
            mt.write_report({"note": f"ключ {TEST_KEY} в отчёте", "candidates": 0}, target)
            raw = target.read_bytes()
            self.assertNotIn(b"TESTKEY", raw)
            self.assertNotIn(b"\r\n", raw)
            self.assertEqual(json.loads(raw.decode("utf-8"))["candidates"], 0)

    def test_display_path_hides_home_directory(self):
        self.assertEqual(mt.display_path(BUNDLED), "app/analyst/cache")
        self.assertEqual(mt.display_path(Path(tempfile.gettempdir()) / "x"), "x")

    def test_script_is_runnable_as_module_without_network(self):
        # импорт модуля не должен требовать ключа или сети
        self.assertTrue(callable(mt.main))
        with NetworkForbidden():
            code, _, _ = self.run_main("--no-write")
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
