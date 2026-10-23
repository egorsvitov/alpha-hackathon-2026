import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from llm.event_pipeline.company_news import contracts as c
from llm.event_pipeline.company_news.contract_fixtures import MODELS, fixture


class SharedContracts(unittest.TestCase):
    def test_fixtures_and_schema_drift(self):
        root = Path(__file__).parents[1]
        data = json.loads((root / "fixtures/shared-input.v1.json").read_text())
        self.assertEqual(c.NewsAnalysisInput.model_validate(data), fixture())
        for model in MODELS:
            stored = json.loads((root / "schemas" / (model.__name__ + ".json")).read_text())
            self.assertEqual(stored, model.model_json_schema())
            Draft202012Validator.check_schema(stored)
        Draft202012Validator(c.NewsAnalysisInput.model_json_schema()).validate(data)

    def test_hash_source_profile_and_reference_rejected(self):
        baseline = fixture().model_dump(mode="json")
        def check(change):
            data = json.loads(json.dumps(baseline));change(data)
            with self.assertRaises((ValidationError, KeyError)):
                c.NewsAnalysisInput.model_validate(data)
        check(lambda d: d["batch"]["documents"][0].update(text="tampered"))
        check(lambda d: d["batch"]["selected_context"][0]["spans"][0].update(end=100000))
        check(lambda d: d["batch"].update(snapshot_id="another-snapshot"))
        check(lambda d: d["batch"].update(profile_version="stale-profile"))
        check(lambda d: d.update(company_facts=[]))

    def test_unknown_dates_and_hypotheses_are_explicit(self):
        c.NewsDocumentRef(document_id="s", content_hash="a"*64, snapshot_id="s", source="s", published_at=None, publication_precision="unknown")
        with self.assertRaises(ValidationError):
            c.NewsDocumentRef(document_id="s", content_hash="a"*64, snapshot_id="s", source="s", published_at="2026-10-23", publication_precision="timestamp")
        fields=fixture().batch.queries[0].model_dump(exclude={"query_id"});fields.update(family="hypotheses",status="observed")
        with self.assertRaises(ValidationError):
            c.company_query(**fields)
