"""Explicit, loss-aware upgrade. No implicit version detection or downgrade."""

from copy import deepcopy
import json


from jsonschema import Draft202012Validator, FormatChecker

from llm.events.contracts.models import Issue
from llm.events.contracts.validation import ContractValidationError, validate_bundle

from llm.events.paths import ROOT


def upgrade_v01(payload):
    schema = json.loads((ROOT / "docs/contracts/event-bundle.schema.json").read_text())
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload))
    if errors:
        raise ContractValidationError([
            Issue(code="invalid_v01", path="/" + "/".join(map(str, e.absolute_path)), message=e.message)
            for e in errors
        ])
    upgraded = deepcopy(payload)
    upgraded["schema_version"] = "0.2"
    for claim in upgraded["claims"]:
        claim["scenario"] = None
        for measurement in claim["measurements"]:
            measurement["value_role"] = "reported"
    return validate_bundle(upgraded)
