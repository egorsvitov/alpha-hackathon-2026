"""Behavior tests use temporary synthetic CSVs; no bank data or network needed."""
import copy
import csv
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ranking.engine import compose_feed, load_rules, rank_events, score_event
from ranking.events import EventError, available_versions, timestamp, validate_event, validate_events
from ranking.graph import attach_payment_graph
from ranking.metrics import evaluate, ndcg_at_k, precision_at_k
from ranking.profiles import CompanyRepository, PRODUCT_FIELDS, ProfileError, industry_for, parse_flag

AS_OF = "2026-10-20T18:00:00+03:00"


def raw_company(uid="A", code="49.41.2", **changes):
    row = {"SYNTHETIC_U": uid, "MAINOKVED_CODE": code, "MAINOKVED_NAME": "Синтетическая компания",
           "REGION": "Регион А", "CITY": "Город А", "REVENUE_COMPANY": "20000000000.0",
           "FTS_FLAG": "0.0", "FTS_COUNTRY_NAME": "", "AMOUNT_WORKERS_FNS_GRADATION": "16-100",
           "CL_TYPE": "CLIENT", **{key: "0.0" for key in PRODUCT_FIELDS.values()}}
    row.update(changes)
    return row


def event(eid="E", **changes):
    raw = {"event_id": eid, "title": "Тестовое событие", "text": "Вымышленный текст для теста.",
           "event_type": "fuel", "source_id": "synthetic://test", "synthetic": True,
           "published_at": "2026-10-20T12:00:00+03:00"}
    raw.update(changes)
    return validate_event(raw)


class RankerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def repository(self, rows):
        path = self.root/"companies.csv"
        fields = sorted({field for row in rows for field in row})
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        return CompanyRepository(path)

    def test_profile_preserves_intervals_unknown_and_provenance(self):
        repo = self.repository([raw_company(CL_PRODUCT_LOA_MARK="", FTS_FLAG="1.0",
                                            FTS_COUNTRY_NAME=" Китай, КИТАЙ , Турция ")])
        p = repo.get("A")
        self.assertIsNone(p["products"]["loan"])
        self.assertFalse(p["products"]["deposit"])
        self.assertEqual(p["employees_band"], "16-100")
        self.assertEqual(p["countries"], ["КИТАЙ", "ТУРЦИЯ"])
        self.assertIsNone(p["revenue"]["unit"])
        self.assertEqual(p["features"]["A:products.loan"]["source"]["data_record_number"], 1)
        self.assertEqual(p["features"]["A:products.loan"]["source"]["file_sha256"], repo.sha256)

    def test_flag_parsing_does_not_turn_unknown_into_false(self):
        for value in ["", None, "bad", "NaN", "2"]:
            self.assertIsNone(parse_flag(value))
        for value in ["0.0", "0", 0, False]:
            self.assertIs(parse_flag(value), False)

    def test_conflicting_id_rejected_and_identical_duplicate_disclosed(self):
        repo = self.repository([raw_company(), raw_company(code="62.01"), raw_company("B"), raw_company("B")])
        with self.assertRaises(ProfileError):
            repo.get("A")
        self.assertIn("identical_duplicate_records_collapsed", repo.get("B")["warnings"])
        with self.assertRaises(ProfileError):
            repo.get("not_there")

    def test_unknown_okved_is_not_guessed(self):
        p = self.repository([raw_company(code="broken")]).get("A")
        self.assertIsNone(p["industry"])
        self.assertEqual(p["okved"]["code"], "broken")
        self.assertIn("industry", p["missing_fields"])
        self.assertIsNone(industry_for("99.99"))

    def test_nonfinite_revenue_is_unknown(self):
        for value in ["NaN", "Infinity", "-1", ""]:
            p = self.repository([raw_company(REVENUE_COMPANY=value)]).get("A")
            self.assertIsNone(p["revenue"]["raw"])

    def test_conflicting_fts_country_does_not_boost_relevance(self):
        p = self.repository([raw_company(FTS_FLAG="0.0", FTS_COUNTRY_NAME="КИТАЙ")]).get("A")
        self.assertEqual(p["features"]["A:countries"]["quality"], "conflicting")
        s = score_event(p, event(event_type="trade_restriction", countries=["КИТАЙ"]), AS_OF)
        self.assertEqual(s["score"], 0)

    def test_fuel_prefers_transport_and_is_conditional(self):
        repo = self.repository([raw_company("A"), raw_company("B", "62.01")])
        a = score_event(repo.get("A"), event(), AS_OF)
        b = score_event(repo.get("B"), event(), AS_OF)
        self.assertGreater(a["score"], b["score"])
        self.assertEqual(a["evidence_status"], "conditional")
        self.assertIn("own_fleet", a["missing_context"])
        self.assertTrue(a["features_used"])
        self.assertIn("no_rule_match", " ".join(b["warnings"]))

    def test_activity_text_can_retrieve_previously_untagged_topic(self):
        p = self.repository([raw_company(code="80.10", MAINOKVED_NAME="Деятельность частных охранных служб")]).get("A")
        e = event(event_type="previously_unseen_topic", title="Новые требования к частным охранным службам")
        s = score_event(p, e, AS_OF)
        self.assertIn("lexical", s["candidate_channels"])
        self.assertIn("A:okved.name", s["features_used"])
        self.assertEqual(s["reasons"][0]["basis"], "lexical_match_only")

    def test_lexical_does_not_override_explicit_nonmatching_okved_scope(self):
        p = self.repository([raw_company(code="46.49", MAINOKVED_NAME="Торговля оптовая бытовыми товарами")]).get("A")
        e = event(event_type="regulation", title="Торговля оптовая лекарствами", okved_prefixes=["46.46"])
        self.assertEqual(score_event(p, e, AS_OF)["score"], 0)

    def test_credit_flag_increases_rate_relevance_but_not_claimed_cost(self):
        repo = self.repository([raw_company("A", CL_PRODUCT_LOA_MARK="1.0"), raw_company("B")])
        e = event(event_type="key_rate")
        a, b = [score_event(repo.get(uid), e, AS_OF) for uid in ["A", "B"]]
        self.assertGreater(a["score"], b["score"])
        self.assertIn("loan_balance", a["missing_context"])
        self.assertIn("A:products.loan", a["features_used"])

    def test_unknown_product_not_treated_as_observed_false(self):
        p = self.repository([raw_company(CL_PRODUCT_LOA_MARK="")]).get("A")
        s = score_event(p, event(event_type="key_rate"), AS_OF)
        self.assertIn("products.loan", s["missing_context"])
        self.assertFalse(any(r["group"] == "products" for r in s["reasons"]))

    def test_okved_prefix_is_hierarchical_not_arbitrary_string_prefix(self):
        repo = self.repository([raw_company("A", "46.49"), raw_company("B", "46.46")])
        e = event(event_type="regulation", okved_prefixes=["46.46"])
        self.assertEqual(score_event(repo.get("A"), e, AS_OF)["score"], 0)
        self.assertGreater(score_event(repo.get("B"), e, AS_OF)["score"], 0)

    def test_steel_does_not_apply_to_all_manufacturing(self):
        repo = self.repository([raw_company("A", "10.11"), raw_company("B", "25.11")])
        e = event(event_type="steel")
        self.assertEqual(score_event(repo.get("A"), e, AS_OF)["score"], 0)
        self.assertGreater(score_event(repo.get("B"), e, AS_OF)["score"], 0)

    def test_local_weather_does_not_assume_all_transport_has_local_routes(self):
        repo = self.repository([raw_company("A", REGION="Регион А"), raw_company("B", REGION="Регион Б")])
        e = event(event_type="weather", regions=["Регион А"])
        self.assertGreater(score_event(repo.get("A"), e, AS_OF)["score"], 0)
        self.assertEqual(score_event(repo.get("B"), e, AS_OF)["score"], 0)

    def test_future_unknown_and_expired_news_not_ranked(self):
        p = self.repository([raw_company()]).get("A")
        items = [event("future", published_at="2026-10-21T00:00:00+03:00"),
                 event("unknown", published_at=None), event("old", published_at="2026-01-01T00:00:00+03:00")]
        result = rank_events(p, items, AS_OF)
        self.assertEqual(result["feed"], [])
        self.assertEqual({e["reason"] for e in result["excluded"]},
                         {"not_yet_published", "unknown_publication_time", "outside_lookback_window"})

    def test_old_regulation_with_upcoming_effect_is_available(self):
        items, _ = available_versions([event(published_at="2026-01-01T00:00:00+03:00",
                                             effective_at="2026-10-22T00:00:00+03:00")], AS_OF)
        self.assertEqual(len(items), 1)

    def test_later_ingestion_does_not_leak_into_historical_feed(self):
        items, excluded = available_versions([event(observed_at="2026-10-21T12:00:00+03:00")], AS_OF)
        self.assertEqual(items, [])
        self.assertEqual(excluded[0]["reason"], "not_yet_observed")

    def test_duplicate_csv_headers_are_rejected(self):
        path = self.root/"duplicate.csv"
        path.write_text("SYNTHETIC_U;SYNTHETIC_U\nA;B\n")
        with self.assertRaises(ProfileError):
            CompanyRepository(path)

    def test_invalid_rule_weights_are_rejected(self):
        rules = load_rules()
        rules["weights"]["industry"] = float("nan")
        path = self.root/"rules.json"
        path.write_text(json.dumps(rules))
        with self.assertRaises(ValueError):
            load_rules(path)

    def test_profile_snapshot_warning_is_visible_in_rank_response(self):
        p = self.repository([raw_company()]).get("A")
        result = rank_events(p, [event()], AS_OF)
        self.assertIn("revenue_unit_unconfirmed", result["profile_warnings"])

    def test_higher_grade_can_be_missed_and_is_reported(self):
        p = self.repository([raw_company()]).get("A")
        items = [event("unsupported", event_type="new_topic")]
        result = rank_events(p, items, AS_OF)
        report = evaluate(result, items, [{"company_id": "A", "cluster_id": "unsupported", "grade": 3}])
        self.assertEqual(report["candidate_recall_relevance_ge_2"], 0)
        self.assertEqual(report["important_grade_3_missed"], ["unsupported"])

    def test_timezones_are_compared_as_instants(self):
        # 16 UTC is later than as_of=15 UTC although the local hour string is smaller.
        current, excluded = available_versions([event(published_at="2026-10-20T16:00:00Z")], AS_OF)
        self.assertEqual(current, [])
        self.assertEqual(excluded[0]["reason"], "not_yet_published")
        with self.assertRaises(EventError):
            timestamp("2026-10-20T18:00:00")

    def test_latest_observable_cluster_version_is_selected(self):
        items = [event("old", cluster_id="cluster", version=1),
                 event("new", cluster_id="cluster", version=2, published_at="2026-10-20T14:00:00+03:00"),
                 event("future", cluster_id="cluster", version=3, published_at="2026-10-21T00:00:00+03:00")]
        current, _ = available_versions(items, AS_OF)
        self.assertEqual([e["event_id"] for e in current], ["new"])

    def test_new_version_does_not_inherit_higher_score_of_old_claim(self):
        p = self.repository([raw_company()]).get("A")
        old = event("old", cluster_id="C", version=1)
        new = event("new", cluster_id="C", version=2, event_type="unsupported",
                    published_at="2026-10-20T14:00:00+03:00")
        result = rank_events(p, [old, new], AS_OF)
        self.assertEqual(result["feed"], [])
        self.assertNotIn("old", [i["event_id"] for i in result["ranked"]])

    def test_conflicting_versions_are_rejected_by_public_rank_api(self):
        p = self.repository([raw_company()]).get("A")
        with self.assertRaises(EventError):
            rank_events(p, [event(), event(event_type="cyber")], AS_OF)
        self.assertEqual(len(validate_events([event(), event()])), 1)

    def test_late_reprint_cannot_override_new_material_version(self):
        events = [event("new", cluster_id="C", version=2, published_at="2026-10-19T12:00:00+03:00"),
                  event("reprint", cluster_id="C", version=1, published_at="2026-10-20T12:00:00+03:00")]
        current, _ = available_versions(events, AS_OF)
        self.assertEqual([e["event_id"] for e in current], ["new"])

    def test_input_validation_rejects_string_tags_and_fake_real_mapping(self):
        with self.assertRaises(EventError):
            event(industries="transport")
        with self.assertRaises(EventError):
            event(synthetic=False, entity_resolution="synthetic")
        with self.assertRaises(EventError):
            event(version=True)

    def test_empty_input_unknown_news_and_invalid_limits(self):
        p = self.repository([raw_company()]).get("A")
        self.assertEqual(rank_events(p, [], AS_OF)["feed"], [])
        self.assertEqual(rank_events(p, [event(event_type="unknown")], AS_OF)["feed"], [])
        with self.assertRaises(ValueError):
            rank_events(p, [event()], AS_OF, top_k=0)

    def test_order_is_deterministic_and_input_is_not_mutated(self):
        p = self.repository([raw_company()]).get("A")
        events = [event("B"), event("A")]
        original = copy.deepcopy(events)
        first = rank_events(p, events, AS_OF)
        second = rank_events(p, list(reversed(events)), AS_OF)
        self.assertEqual([s["event_id"] for s in first["feed"]], [s["event_id"] for s in second["feed"]])
        self.assertEqual(events, original)
        self.assertEqual(first["input_events_sha256"], second["input_events_sha256"])

    def test_rule_hash_changes_even_if_human_version_was_not_bumped(self):
        p = self.repository([raw_company()]).get("A")
        rules = load_rules()
        first = rank_events(p, [event()], AS_OF, rules=rules)
        rules["weights"]["freshness"] += 1
        second = rank_events(p, [event()], AS_OF, rules=rules)
        self.assertNotEqual(first["rules_sha256"], second["rules_sha256"])

    def test_every_explanation_reference_resolves_to_original_feature(self):
        p = self.repository([raw_company(CL_PRODUCT_LOA_MARK="1.0", FTS_FLAG="1.0", FTS_COUNTRY_NAME="КИТАЙ")]).get("A")
        e = event(event_type="key_rate", countries=["КИТАЙ"], regions=["Регион А"],
                  industries=["transport"], entity_ids=["A"], entity_resolution="synthetic")
        score = score_event(p, e, AS_OF)
        self.assertTrue(math.isfinite(score["score"]))
        self.assertLessEqual(score["score"], 100)
        for reason in score["reasons"]:
            for ref in reason["feature_ids"]:
                self.assertEqual(score["features_used"][ref], p["features"][ref])

    def test_no_entity_matching_without_explicit_resolution(self):
        p = self.repository([raw_company()]).get("A")
        unresolved = score_event(p, event(event_type="counterparty", entity_ids=["A"]), AS_OF)
        self.assertEqual(unresolved["score"], 0)
        resolved = score_event(p, event(event_type="counterparty", entity_ids=["A"], entity_resolution="synthetic"), AS_OF)
        self.assertGreater(resolved["score"], 0)
        self.assertIn("entity_match_is_synthetic_test_only", resolved["warnings"])

    def test_seen_version_is_hidden_but_update_is_visible(self):
        p = self.repository([raw_company()]).get("A")
        original = rank_events(p, [event(cluster_id="C")], AS_OF, seen_versions={"C": 1})
        self.assertEqual(original["feed"], [])
        updated = rank_events(p, [event(cluster_id="C", version=2)], AS_OF, seen_versions={"C": 1})
        self.assertEqual(len(updated["feed"]), 1)

    def test_topic_diversity_and_high_priority_override(self):
        p = self.repository([raw_company()]).get("A")
        items = [score_event(p, event(str(i)), AS_OF) for i in range(4)]
        self.assertEqual(len(compose_feed(items, 5)), 2)
        items[2]["score"] = 90
        self.assertEqual(len(compose_feed(items, 5)), 2)  # high item first, topic budget still two
        for item in items:
            item["score"] = 90
        self.assertEqual(len(compose_feed(items, 5)), 4)

    def test_graph_retains_unknown_nodes_and_excludes_ambiguous_pairs(self):
        repo = self.repository([raw_company("A"), raw_company("B")])
        path = self.root/"payments.csv"
        path.write_text("PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\nA;B;90;1\nA;C;10;2\nB;A;20;1\nA;A;1000;1\nA;D;5;1\nA;D;7;1\nX;Y;8;1\nA;Z;NaN;1\n")
        source = repo.get("A")
        profiles, stats = attach_payment_graph([source], repo.company_ids, path)
        graph = profiles[0]["graph"]
        self.assertEqual(source["graph"]["status"], "not_loaded")
        self.assertNotEqual(source["profile_version"], profiles[0]["profile_version"])
        self.assertEqual(graph["profile_coverage_by_nodes"], .5)
        out = {n["company_id"]: n for n in graph["neighbors"] if n["direction"] == "outgoing"}
        self.assertEqual(set(out), {"B", "C"})
        self.assertAlmostEqual(out["B"]["observed_share"], .9)
        self.assertFalse(out["C"]["profile_known"])
        self.assertEqual(stats["self_loops_excluded"], 1)
        self.assertEqual(stats["ambiguous_pair_rows_excluded"], 2)
        self.assertEqual(stats["invalid_selected_rows"], 1)
        score = score_event(profiles[0], event(event_type="counterparty", entity_ids=["C"],
                                               entity_resolution="synthetic"), AS_OF)
        self.assertGreater(score["score"], 0)
        self.assertTrue(any("graph.outgoing.C" in ref for ref in score["features_used"]))

    def test_metrics_match_hand_calculation_and_zero_group_is_not_perfect(self):
        grades = {"A": 3, "B": 2, "C": 0}
        self.assertAlmostEqual(ndcg_at_k(["A", "B", "C"], grades, 2), 1)
        expected = (3+7/math.log2(3))/(7+3/math.log2(3))
        self.assertAlmostEqual(ndcg_at_k(["B", "A"], grades, 2), expected)
        self.assertIsNone(ndcg_at_k(["C"], {"C": 0}))
        self.assertEqual(precision_at_k(["A"], grades, 5), .2)

    def test_invalid_repeated_payment_does_not_rescue_ambiguous_pair(self):
        repo = self.repository([raw_company()])
        path = self.root/"payments.csv"
        path.write_text("PAYER;BENEFICIARY;PAY_AMT;PAY_CNT\nA;B;10;1\nA;B;NaN;1\n")
        profiles, stats = attach_payment_graph([repo.get("A")], repo.company_ids, path)
        self.assertEqual(profiles[0]["graph"]["neighbors"], [])
        self.assertEqual(stats["ambiguous_pair_rows_excluded"], 2)

    def test_unjudged_events_do_not_become_negative_labels(self):
        p = self.repository([raw_company()]).get("A")
        events = [event("A"), event("B")]
        result = rank_events(p, events, AS_OF)
        values = evaluate(result, events, [{"company_id": "A", "cluster_id": "A", "grade": 2}])
        self.assertEqual(values["ungraded_clusters"], ["B"])
        self.assertIsNone(values["metrics"]["feed"]["ndcg_at_5"])
        with self.assertRaises(ValueError):
            evaluate(result, events, [{"company_id": "A", "cluster_id": "A", "grade": 4}])

    def test_cli_works_without_bank_data_on_synthetic_csv(self):
        repo = self.repository([raw_company()])
        events_file = self.root/"events.json"
        events_file.write_text(json.dumps([event()]))
        output = self.root/"out.json"
        command = [sys.executable, "-m", "ranking", "rank", "--companies-csv", str(repo.path),
                   "--company-id", "A", "--events", str(events_file), "--as-of", AS_OF, "--output", str(output)]
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(output.read_text())["feed"][0]["event_id"], "E")
        failed = subprocess.run(command+ ["--top-k", "0"], capture_output=True, text=True)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("положительным", failed.stderr)


if __name__ == "__main__":
    unittest.main()
