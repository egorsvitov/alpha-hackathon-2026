import copy
import json
import subprocess
import sys
import unittest

from jsonschema import Draft202012Validator, FormatChecker

from llm.events.compatibility import upgrade_v01
from llm.events.models import ArticleInput, EventType
from typing import get_args
from llm.events.schemas import ROOT, generated_schemas
from llm.events.validation import (
    ContractValidationError, article_sha256, pointer, validate_bundle,
    validate_gold, validate_grounding,
)


def load(relative):
    return json.loads((ROOT / relative).read_text())


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gold = load("llm/events/fixtures/synthetic.json")
        cls.records = cls.gold["articles"]
        cls.examples = {p.stem: json.loads(p.read_text()) for p in (ROOT / "docs/b6-examples/v0.2").glob("*.json")}
        cls.schema = Draft202012Validator(load("docs/contracts/event-bundle.v0.2.schema.json"), format_checker=FormatChecker())

    def sample(self, key="normal"):
        return copy.deepcopy(self.examples[key])

    def rejected(self, payload, code):
        with self.assertRaises(ContractValidationError) as caught:
            validate_bundle(payload)
        self.assertIn(code, [i.code for i in caught.exception.issues])
        for issue in caught.exception.issues:
            self.assertTrue(issue.path.startswith("/"))
            self.assertTrue(issue.message)

    def test_schemas_are_generated_from_models(self):
        for path, schema in generated_schemas():
            Draft202012Validator.check_schema(schema)
            self.assertEqual(json.loads(path.read_text()), schema)

    def test_v01_examples_and_explicit_upgrade(self):
        old = Draft202012Validator(load("docs/contracts/event-bundle.schema.json"), format_checker=FormatChecker())
        for path in (ROOT / "docs/b6-examples").glob("*.json"):
            payload = json.loads(path.read_text())
            unchanged = copy.deepcopy(payload)
            old.validate(payload)
            upgraded = upgrade_v01(payload)
            self.assertEqual(unchanged, payload)
            self.assertTrue(all(c.scenario is None for c in upgraded.claims))
            self.assertTrue(all(m.value_role == "reported" for c in upgraded.claims for m in c.measurements))
            self.assertEqual(upgraded.claims[0].time.model_dump(mode="json"), payload["claims"][0]["time"])
        with self.assertRaises(ContractValidationError):
            upgrade_v01(self.examples["normal"])

    def test_legacy_not_accepted_implicitly(self):
        self.rejected(load("docs/b6-examples/normal.json"), "invalid_type_or_value")

    def test_all_v02_examples_and_grounding(self):
        records = load("llm/events/fixtures/b6_articles.json")
        self.assertEqual(len(self.examples), 7)
        for data in self.examples.values():
            self.schema.validate(data)
            validate_grounding(data, records)

    def test_twenty_synthetic_articles(self):
        self.assertEqual(len(self.records), 20)
        gold = validate_gold(self.gold)
        for b in gold.bundles:
            self.schema.validate(b.model_dump(mode="json"))

    def test_identity_vocabulary_covers_every_event_type(self):
        vocabulary=load("llm/events/vocabulary.json")
        self.assertEqual(set(vocabulary["identity_by_type"]),set(get_args(EventType)))
        self.assertTrue(all(vocabulary["identity_by_type"].values()))
        cbr=next(e for e in vocabulary["entity_aliases"] if e["entity_id"]=="org:bank_of_russia")
        self.assertIn("ЦБ",cbr["aliases"])
        self.assertTrue(cbr["context_rule"])

    def test_model_forbids_extras_and_coercion(self):
        for mutate in [lambda p: p.update(company_id="invented"),
                       lambda p: p["event"].update(event_version="1"),
                       lambda p: p["event"].update(event_version=True),
                       lambda p: p["claims"][0]["measurements"][0].update(value=14.0)]:
            data = self.sample(); mutate(data)
            self.assertFalse(self.schema.is_valid(data))
            self.rejected(data, "invalid_type_or_value")

    def test_new_fields_are_required(self):
        data = self.sample(); del data["claims"][0]["scenario"]
        self.rejected(data, "invalid_type_or_value")
        data = self.sample(); del data["claims"][0]["measurements"][0]["value_role"]
        self.rejected(data, "invalid_type_or_value")

    def test_dates_and_explicit_timezone(self):
        for stamp in ["2026-10-20T12:00:00", "2026-02-30T12:00:00+03:00"]:
            data = self.sample(); data["event"]["updated_at"] = stamp
            self.rejected(data, "invalid_type_or_value")
        data = self.sample(); data["claims"][0]["time"]["occurred_at"]["start"] = "2026-02-30"
        self.rejected(data, "invalid_type_or_value")

    def test_reversed_period_and_range(self):
        data = self.sample(); data["claims"][0]["time"]["occurred_at"]["end"] = "2025-01-01"
        self.rejected(data, "reversed_period")
        data = self.sample(); data["claims"][0]["measurements"][0]["value"] = {"min":"15","max":"14"}
        self.rejected(data, "reversed_range")

    def test_range_qualifier(self):
        data = self.sample(); data["claims"][0]["measurements"][0].update(value={"min":"13","max":"14"},qualifier="gt")
        self.rejected(data, "invalid_range_qualifier")

    def test_number_strings_no_exponents_or_infinity(self):
        for value in ["NaN", "Infinity", "1e9", "14,0", "01", ""]:
            data = self.sample(); data["claims"][0]["measurements"][0]["value"] = value
            self.rejected(data, "invalid_type_or_value")

    def test_identifiers_and_dangling_evidence(self):
        data = self.sample(); data["evidence"].append(copy.deepcopy(data["evidence"][0]))
        self.rejected(data, "duplicate_id")
        data = self.sample(); data["claims"][0]["evidence_ids"] = ["missing"]
        self.rejected(data, "missing_evidence")
        data = self.sample(); data["evidence"][0]["source_id"] = "missing"
        self.rejected(data, "missing_source")

    def test_missing_and_invalid_field_provenance(self):
        data = self.sample(); del data["claims"][0]["field_evidence"]["/measurements/0"]
        self.rejected(data, "uncovered_field")
        data = self.sample(); data["claims"][0]["field_evidence"]["/measurements/-1"] = data["claims"][0]["evidence_ids"]
        self.rejected(data, "invalid_pointer")

    def test_json_pointer_escapes(self):
        self.assertEqual(pointer({"a/b": {"~c": ["yes"]}}, "/a~1b/~0c/0"), "yes")
        for path in ["/a~2b", "/a/01", "/a/-1"]:
            with self.assertRaises((ValueError,KeyError)):
                pointer({"a":[1]}, path)

    def test_multiple_sources_in_one_claim_rejected(self):
        data = self.sample("conflict")
        data["claims"][0]["evidence_ids"].append(data["claims"][1]["evidence_ids"][0])
        self.rejected(data, "multiple_claim_sources")

    def test_conflict_variant_references(self):
        data = self.sample("conflict"); data["conflicts"][0]["variants"][0]["field_path"] = "/measurements/9/value"
        self.rejected(data, "invalid_conflict_reference")
        data = self.sample("conflict"); data["claims"][1]["measurements"][0]["value"] = "20"
        self.rejected(data, "identical_conflict_variants")

    def test_scenarios_are_not_conflicts(self):
        data = self.sample("scenarios")
        validate_bundle(data)
        self.assertEqual([c["scenario"]["kind"] for c in data["claims"]], ["baseline", "alternative"])
        data["conflicts"] = [{"conflict_id":"wrong","description":"False conflict","variants":[
            {"claim_id":c["claim_id"],"field_path":"/measurements/0/value"} for c in data["claims"]]}]
        self.rejected(data, "different_scenarios")

    def test_before_after_are_not_conflict_variants(self):
        data = self.sample("before-after")
        cid = data["claims"][0]["claim_id"]
        data["conflicts"] = [{"conflict_id":"wrong","description":"Before and after differ","variants":[
            {"claim_id":cid,"field_path":f"/measurements/{i}/value"} for i in (0,1)]}]
        self.rejected(data,"different_measurement_scope")

    def test_duplicate_labels_are_rejected_by_model_and_schema(self):
        data = self.sample(); data["ranking_features"]["topics"] = ["rates","rates"]
        self.assertFalse(self.schema.is_valid(data))
        self.rejected(data,"invalid_type_or_value")

    def test_relations_and_archive_updates(self):
        corrected = next(b for b in self.gold["bundles"] if b["event"]["event_id"] == "synthetic:corrected")
        validate_bundle(corrected)
        self.assertTrue(corrected["relations"][0]["to_id"].startswith("archive:"))
        data = self.sample(); data["relations"] = [{"type":"duplicate_claim","from_id":data["claims"][0]["claim_id"],"to_id":"missing","target_kind":"claim"}]
        self.rejected(data,"missing_claim")
        data["relations"][0]["target_kind"] = "event"
        self.rejected(data,"invalid_relation_kind")

    def test_empty_active_event_and_partial_limitation(self):
        data = self.sample(); data["claims"] = []
        self.rejected(data,"empty_active_event")
        data = self.sample("partial"); data["limitations"] = []
        self.rejected(data,"missing_limitation")
        data = self.sample(); data["event"]["quality"] = "insufficient"; data["claims"] = []
        validate_bundle(data)
        data["event"]["availability"] = "withdrawn"
        validate_bundle(data)

    def test_grounding_requires_source(self):
        with self.assertRaises(ContractValidationError) as caught:
            validate_grounding(self.sample(), [])
        self.assertIn("missing_article", [i.code for i in caught.exception.issues])

    def test_exact_quote_is_independent_from_structure(self):
        data = self.sample(); data["evidence"][0]["quote"] = "X" + data["evidence"][0]["quote"][1:]
        validate_bundle(data)
        with self.assertRaises(ContractValidationError) as caught:
            validate_grounding(data,load("llm/events/fixtures/b6_articles.json"))
        self.assertIn("quote_mismatch",[i.code for i in caught.exception.issues])

    def test_wrong_article_version(self):
        records = load("llm/events/fixtures/b6_articles.json")
        records[0]["article_version"] += 1
        with self.assertRaises(ContractValidationError) as caught:
            validate_grounding(self.sample("conflict"),records)
        self.assertIn("source_version_mismatch",[i.code for i in caught.exception.issues])

    def test_unicode_code_points_not_utf16(self):
        data = copy.deepcopy(next(b for b in self.gold["bundles"] if b["event"]["event_id"] == "synthetic:corrected"))
        e = data["evidence"][0]
        self.assertTrue(e["quote"].startswith("📰 "))
        e["quote"] = e["quote"][2:]; e["start"] = 2
        validate_grounding(data,self.records)
        e["start"] += 1; e["end"] += 1  # UTF-16-based coordinates, same length.
        validate_bundle(data)
        with self.assertRaises(ContractValidationError):
            validate_grounding(data,self.records)

    def test_multi_sentence_evidence(self):
        data = self.sample("cross-sentence")
        self.assertEqual(len(data["claims"][0]["evidence_ids"]),2)
        validate_grounding(data,load("llm/events/fixtures/b6_articles.json"))

    def test_original_time_expression(self):
        data = self.sample(); data["claims"][0]["time"]["occurred_at"]["text"] = "2026-10-20"
        with self.assertRaises(ContractValidationError) as caught:
            validate_grounding(data,load("llm/events/fixtures/b6_articles.json"))
        self.assertIn("expression_not_quoted",[i.code for i in caught.exception.issues])

    def test_representations_keep_roles_units_and_forecasts(self):
        ms=self.examples["before-after"]["claims"][0]["measurements"]
        self.assertEqual([(m["value_role"],m["value"]) for m in ms],[("before","1100000000000"),("after","1096000000000")])
        self.assertEqual(self.examples["forecast"]["claims"][0]["modality"],"forecast")
        self.assertIsNone(self.examples["forecast"]["claims"][0]["time"]["occurred_at"])
        arrears=next(b for b in self.gold["bundles"] if b["event"]["event_id"]=="synthetic:arrears")
        self.assertEqual([m["unit"] for m in arrears["claims"][0]["measurements"]],["percentage_point","percent"])

    def test_hash_detects_text_and_metadata_changes(self):
        record=ArticleInput.model_validate(self.records[0]["article"])
        old=article_sha256(record)
        changed=record.model_copy(update={"title":"Changed"})
        self.assertNotEqual(old,article_sha256(changed))
        data=copy.deepcopy(self.gold); data["articles"][0]["article"]["text"] += " Changed."
        with self.assertRaises(ContractValidationError): validate_gold(data)

    def test_gold_full_corpus_coverage(self):
        corpus=[r["article"] for r in self.records]
        validate_gold(self.gold,corpus)
        with self.assertRaises(ContractValidationError): validate_gold(self.gold,corpus[:-1])

    def test_identity_labels_preserve_different_authors_and_meetings(self):
        expected={(x["from_claim_id"],x["to_claim_id"]):x["relation"] for x in self.gold["expected_links"]}
        self.assertEqual(expected["s04:c1","s16:c1"],"different_event")
        self.assertEqual(expected["s01:c1","s17:c1"],"different_event")
        self.assertEqual(expected["s04:c1","s05:c1"],"different_event")

    def test_offline_cli(self):
        result=subprocess.run([sys.executable,"-m","llm.events.check_contracts"],cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn("20 articles",result.stdout)


if __name__ == "__main__":
    unittest.main()
