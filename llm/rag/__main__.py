"""Validated local retrieval CLI: python -m llm.rag retrieve."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator, FormatChecker
from ranking.b6 import decode_json
from ranking.contracts import require_valid_contract
from .corpus import Corpus
from .retriever import load_config, retrieve_evidence


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    retrieve = sub.add_parser("retrieve")
    for flag in ("event", "profiles", "company-id", "relevance", "as-of", "corpus"):
        retrieve.add_argument("--" + flag, required=True)
    retrieve.add_argument("--output")
    retrieve.add_argument("--config-dir")
    retrieve.add_argument("--base-dir")
    retrieve.add_argument("--attempt", type=int, default=0)
    retrieve.add_argument("--retry-reason")
    args = parser.parse_args()
    try:
        bundle = decode_json(Path(args.event).read_text())
        profiles = decode_json(Path(args.profiles).read_text())
        relevance = decode_json(Path(args.relevance).read_text())
        require_valid_contract(bundle, "event-bundle")
        require_valid_contract(profiles, "company-profiles")
        require_valid_contract(relevance, "relevance")
        profile = next(p for p in profiles["profiles"] if p["company_id"] == args.company_id)
        for key, value in (("event_id", bundle["event"]["event_id"]), ("event_version", bundle["event"]["event_version"]),
                           ("company_id", profile["company_id"]), ("profile_version", profile["profile_version"])):
            if relevance[key] != value:
                raise ValueError(f"relevance {key} does not match pinned input")
        config = load_config(args.config_dir)
        corpus = Corpus(args.corpus, config=config, base_dir=args.base_dir)
        result = retrieve_evidence(bundle, profile, relevance, as_of=datetime.fromisoformat(args.as_of),
                                   corpus=corpus, config=config, attempt=args.attempt, retry_reason=args.retry_reason)
        schema_path = Path(__file__).resolve().parents[2] / "docs/contracts/evidence-bundle.schema.json"
        Draft202012Validator(json.loads(schema_path.read_text()), format_checker=FormatChecker()).validate(result)
        output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(output)
        else:
            sys.stdout.write(output)
        return 0
    except (ValueError, OSError, StopIteration, KeyError) as exc:
        print(f"retrieval error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
