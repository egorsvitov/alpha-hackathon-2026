"""Validate generated schemas and core fixture contracts without network."""

from pathlib import Path
import json
from jsonschema import Draft202012Validator
from . import models

NAMES = (
    "SourceBlock",
    "EventMention",
    "ExtractionAudit",
    "MembershipDecision",
    "EventCluster",
    "RunManifest",
)


def schemas():
    return {n: getattr(models, n).model_json_schema() for n in NAMES}


def check():
    for name, schema in schemas().items():
        Draft202012Validator.check_schema(schema)
        path = Path(__file__).with_name("schemas") / (name + ".json")
        if json.loads(path.read_text()) != schema:
            raise ValueError("schema_out_of_date:" + name)
    cases = json.loads(
        Path(__file__).with_name("fixtures").joinpath("identity_cases.json").read_text()
    )
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("duplicate_fixture")
    for c in cases:
        if c["identity"] not in ("same", "different", "uncertain") or c[
            "compatibility"
        ] not in ("compatible", "conflicting", "unknown"):
            raise ValueError("invalid_fixture")
    return {"schemas": len(NAMES), "identity_cases": len(cases), "status": "ok"}
