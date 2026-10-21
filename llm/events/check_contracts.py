"""Offline contract and annotation verification. Does not measure NLP accuracy."""

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError as SchemaError

from .schemas import ROOT, generated_schemas
from .validation import ContractValidationError, validate_gold, validate_grounding


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, help="Additional local GoldSet JSON")
    parser.add_argument("--articles", type=Path, help="Original input corpus to verify hashes and full coverage")
    args = parser.parse_args()
    if args.articles and not args.gold:
        parser.error("--articles requires --gold")
    try:
        for path, schema in generated_schemas():
            Draft202012Validator.check_schema(schema)
            if not path.exists() or json.loads(path.read_text()) != schema:
                parser.exit(1, f"Schema drift: {path}\n")
        schema = json.loads((ROOT / "docs/contracts/event-bundle.schema.json").read_text())
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for path in sorted((ROOT / "docs/b6-examples").glob("*.json")):
            validator.validate(json.loads(path.read_text()))
        new_schema = json.loads((ROOT / "docs/contracts/event-bundle.v0.2.schema.json").read_text())
        new_validator = Draft202012Validator(new_schema, format_checker=FormatChecker())
        records = json.loads((ROOT / "llm/events/fixtures/b6_articles.json").read_text())
        for path in sorted((ROOT / "docs/b6-examples/v0.2").glob("*.json")):
            payload = json.loads(path.read_text())
            new_validator.validate(payload)
            validate_grounding(payload, records)
        synthetic = ROOT / "llm/events/fixtures/synthetic.json"
        datasets = [synthetic] + ([args.gold] if args.gold else [])
        for path in datasets:
            corpus = json.loads(args.articles.read_text()) if path == args.gold and args.articles else None
            gold = validate_gold(json.loads(path.read_text()), corpus=corpus)
            for bundle in gold.bundles:
                new_validator.validate(bundle.model_dump(mode="json"))
            print(f"PASS {path}: {len(gold.articles)} articles, {sum(len(b.claims) for b in gold.bundles)} claims, {len(gold.bundles)} events")
    except ContractValidationError as exc:
        print(json.dumps({"status": "invalid", "issues": [x.model_dump() for x in exc.issues]}, ensure_ascii=False, indent=2))
        return 1
    except SchemaError as exc:
        print(json.dumps({"status": "invalid", "issues": [{"code": "invalid_schema_value",
            "path": "/" + "/".join(map(str, exc.absolute_path)), "message": exc.message}]}, ensure_ascii=False, indent=2))
        return 1
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Input error: {exc}\n")
    print("Contract integrity and exact excerpts checked; semantic annotations remain single-annotator drafts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
