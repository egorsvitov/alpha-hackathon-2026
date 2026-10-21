"""Cross-module acceptance: identical ingestion, profiles, API and CLI ranking."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ranking.b6 import load_bundles
from ranking.contracts import FixtureClock, parse_timestamp, require_valid_contract
from ranking.engine import load_rules, rank_events
from ranking.event_store import EventStore, ingest_bundles
from ranking.graph import attach_payment_graph


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "docs/b6-examples/normal.json"
PROFILES = ROOT / "data/examples/personalization/profiles.json"
RECEIVED = "2026-10-20T17:00:00+03:00"
AS_OF = "2026-10-20T18:00:00+03:00"


class PersonalizationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.profiles = json.loads(PROFILES.read_text(encoding="utf-8"))["profiles"]
        self.rules = load_rules()

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "ranking", *map(str, arguments)],
            cwd=ROOT, text=True, capture_output=True, timeout=45,
        )

    def rank(self, store, profile, cutoff=AS_OF):
        as_of = parse_timestamp(cutoff)
        result = rank_events(
            store.list_latest(as_of=as_of), profile,
            role="cfo", as_of=as_of, rules=self.rules,
        )
        require_valid_contract(result, "ranking-result")
        return result

    def test_one_snapshot_three_profiles_have_different_supported_reasons(self):
        with EventStore(self.directory / "test.sqlite3") as store:
            ingest_bundles(load_bundles(INPUT), store=store,
                           clock=FixtureClock(RECEIVED), source_mode="fixture")
            outputs = {p["company_id"]: self.rank(store, p) for p in self.profiles}
        self.assertEqual(len(outputs), 3)
        self.assertEqual(len({r["input_snapshot_hash"] for r in outputs.values()}), 1)
        scores = {}
        for profile in self.profiles:
            result = outputs[profile["company_id"]]
            for row in result["ranked"]:
                rel = row["relevance"]
                self.assertEqual(rel["profile_version"], profile["profile_version"])
                self.assertTrue(set(rel["feature_ids"]) <= profile["features"].keys())
                self.assertAlmostEqual(sum(r["contribution"] for r in rel["reasons"]), rel["score"])
            scores[profile["company_id"]] = max(
                (row["relevance"]["score"] for row in result["ranked"]), default=0,
            )
        self.assertGreater(scores["DEMO_LOGISTICS"], scores["DEMO_IT"])
        self.assertGreater(scores["DEMO_TRADE"], scores["DEMO_IT"])
        self.assertTrue(any(
            fid.endswith("/products/leasing")
            for row in outputs["DEMO_LOGISTICS"]["ranked"]
            for fid in row["relevance"]["feature_ids"]
        ))
        self.assertTrue(any(
            fid.endswith("/products/loan")
            for row in outputs["DEMO_TRADE"]["ranked"]
            for fid in row["relevance"]["feature_ids"]
        ))

    def test_fixture_import_cli_matches_python_api(self):
        target = next(p for p in self.profiles if p["company_id"] == "DEMO_TRADE")
        with EventStore(self.directory / "api.sqlite3") as store:
            ingest_bundles(load_bundles(INPUT), store=store,
                           clock=FixtureClock(RECEIVED), source_mode="fixture")
            expected = self.rank(store, target)
        completed = self.cli(
            "rank", "--events", INPUT, "--profiles", PROFILES,
            "--company-id", "DEMO_TRADE", "--role", "cfo", "--as-of", AS_OF,
            "--mode", "fixture", "--fixture-clock", RECEIVED,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout), expected)

    def test_ingest_cli_state_is_read_by_rank_cli_and_respects_receipt(self):
        state = self.directory / "state"
        completed = self.cli("ingest", "--input", INPUT, "--state-dir", state,
                             "--mode", "fixture", "--fixture-clock", RECEIVED)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        common = ("rank", "--state-dir", state, "--profiles", PROFILES,
                  "--company-id", "DEMO_TRADE", "--role", "cfo", "--mode", "fixture")
        before = self.cli(*common, "--as-of", "2026-10-20T16:00:00+03:00")
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        self.assertEqual(json.loads(before.stdout)["ranked"], [])
        after = self.cli(*common, "--as-of", AS_OF)
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        ranked = json.loads(after.stdout)["ranked"]
        self.assertEqual(len(ranked), 1, "rank must read the database actually written by ingest")
        self.assertEqual(ranked[0]["snapshot"]["received_at"], parse_timestamp(RECEIVED).isoformat())

    def test_withdrawal_does_not_resurrect_active_version(self):
        original = load_bundles(INPUT)[0]
        withdrawn = deepcopy(original)
        withdrawn["event"].update(event_version=2, availability="withdrawn")
        target = next(p for p in self.profiles if p["company_id"] == "DEMO_TRADE")
        with EventStore(self.directory / "withdrawal.sqlite3") as store:
            ingest_bundles([original], store=store, clock=FixtureClock(RECEIVED), source_mode="fixture")
            ingest_bundles([withdrawn], store=store,
                           clock=FixtureClock("2026-10-20T17:30:00+03:00"), source_mode="fixture")
            before = self.rank(store, target, "2026-10-20T17:15:00+03:00")
            after = self.rank(store, target)
        self.assertEqual(before["ranked"][0]["snapshot"]["event_version"], 1)
        self.assertEqual(after["feed"], [])
        self.assertEqual(after["ranked"], [])
        self.assertEqual(after["excluded"][0]["snapshot"]["event_version"], 2)
        self.assertIn("event_withdrawn", after["excluded"][0]["reason_codes"])

    def test_live_rank_rejects_fixture_import_and_clock(self):
        completed = self.cli(
            "rank", "--events", INPUT, "--profiles", PROFILES,
            "--company-id", "DEMO_TRADE", "--mode", "live",
            "--fixture-clock", RECEIVED,
        )
        self.assertNotEqual(completed.returncode, 0)

    def test_actual_graph_output_controls_counterparty_ranking(self):
        """Exercise real attach → feature provenance → resolver → score, not a mock graph."""
        company = next(p for p in self.profiles if p["company_id"] == "DEMO_LOGISTICS")
        partner = "DEMO_PARTNER"
        bundle = deepcopy(load_bundles(INPUT)[0])
        text = "Учебный контрагент сообщил об остановке работы."
        bundle["event"].update(event_id="synthetic:partner-stop", event_type="operational_incident", title=text)
        bundle["ranking_features"] = {
            "topics": ["counterparty"], "industries": [], "geographies": ["RU"],
            "objects": ["operations"], "metrics": [], "factors": [],
        }
        source = bundle["sources"][0]
        source["title"] = text
        evidence = bundle["evidence"][0]
        evidence.update(quote=text, start=0, end=len(text))
        evidence_ids = [evidence["evidence_id"]]
        bundle["claims"] = [{
            "claim_id": "synthetic:partner-stop:claim", "text": text,
            "modality": "asserted", "polarity": "affirmed", "stage": "observed",
            "attribution": None,
            "participants": [{"entity_id": "news:demo-partner", "name": "Учебный контрагент", "role": "affected_company"}],
            "action": "halt", "object": "operations", "geographies": ["RU"],
            "time": {"occurred_at": None, "effective_at": None, "observation_period": None, "forecast_horizon": None},
            "measurements": [], "evidence_ids": evidence_ids,
            "field_evidence": {path: evidence_ids for path in (
                "/text", "/modality", "/polarity", "/stage", "/participants", "/action", "/object", "/geographies",
            )},
        }]
        mapping = {"resolver_version": "synthetic-integration-v1", "entries": [{
            "news_entity_id": "news:demo-partner", "company_id": partner,
            "status": "synthetic", "provenance": "Fully fictional integration fixture", "scope": "fixture",
        }]}
        profiles = []
        for limited in (False, True):
            csv_path = self.directory / ("limited.csv" if limited else "clean.csv")
            rows = [f"{company['company_id']};{partner};80;1", f"{company['company_id']};DEMO_OTHER;20;1"]
            if limited:
                rows.extend([f"{company['company_id']};DEMO_AMBIGUOUS;100;1"] * 2)
            csv_path.write_text("PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\n" + "\n".join(rows) + "\n", encoding="utf-8")
            attached, _ = attach_payment_graph(
                [company], {company["company_id"], partner}, csv_path,
                config={"state_dir": self.directory / "artifacts-state"},
            )
            require_valid_contract(attached[0], "company-profile")
            profiles.append(attached[0])
        groups = []
        with EventStore(self.directory / "graph.sqlite3") as store:
            ingest_bundles([bundle], store=store, clock=FixtureClock(RECEIVED), source_mode="fixture")
            for profile in profiles:
                as_of = parse_timestamp(AS_OF)
                result = rank_events(store.list_latest(as_of=as_of), profile, role="cfo", as_of=as_of,
                                     rules=self.rules, entity_mapping=mapping)
                require_valid_contract(result, "ranking-result")
                relevance = result["ranked"][0]["relevance"]
                groups.append(next(g for g in relevance["scoring_diagnostics"]["groups"] if g["group"] == "counterparty"))
                for fid in relevance["feature_ids"]:
                    self.assertIn(fid, profile["features"])
        self.assertEqual(groups[0]["raw_points"], 48)
        self.assertEqual(groups[1]["raw_points"], 5)
        self.assertEqual(profiles[0]["graph"]["graph_coverage"]["outgoing"]["share_quality"], "clean_observed")
        self.assertEqual(profiles[1]["graph"]["graph_coverage"]["outgoing"]["share_quality"], "limited")
        self.assertNotEqual(profiles[0]["profile_version"], profiles[1]["profile_version"])


if __name__ == "__main__":
    unittest.main()
