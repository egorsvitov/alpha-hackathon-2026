"""Offline-first personalization CLI; optional feature modules register hooks."""
import argparse
import importlib
import json
import sys
from .b6 import load_bundles, validate_bundle
from .contracts import ContractError, FixtureClock, SystemClock, PROJECT_ROOT
from .event_store import EventStore, ingest_bundles

def _validate(args):
    reports = [{"bundle_index":i,"line":getattr(b,"source_line",None),"valid":not any(x.severity == "error" for x in issues),"issues":[x.to_dict() for x in issues]} for i,b in enumerate(load_bundles(args.input)) for issues in [validate_bundle(b)]]
    print(json.dumps({"valid":all(r["valid"] for r in reports),"reports":reports},ensure_ascii=False))
    return 0 if all(r["valid"] for r in reports) else 1

def _ingest(args):
    from pathlib import Path
    if args.fixture_clock and args.mode != "fixture":
        raise ValueError("--fixture-clock requires --mode fixture")
    if args.mode == "fixture" and not args.fixture_clock:
        raise ValueError("fixture mode requires --fixture-clock")
    directory = Path(args.state_dir)
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    clock = FixtureClock(args.fixture_clock) if args.mode == "fixture" else SystemClock()
    db = directory / ("fixture-events.sqlite3" if args.mode == "fixture" else "events.sqlite3")
    with EventStore(db) as store:
        results = ingest_bundles(load_bundles(args.input),store=store,clock=clock,source_mode=args.mode)
    print(json.dumps({"results":[r.to_dict() for r in results]},ensure_ascii=False))
    return 0

def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m ranking")
    sub = parser.add_subparsers(dest="command",required=True)
    validate = sub.add_parser("validate-b6")
    validate.add_argument("--input",required=True)
    validate.set_defaults(func=_validate)
    ingest = sub.add_parser("ingest")
    ingest.add_argument("--input",required=True)
    ingest.add_argument("--state-dir",required=True)
    ingest.add_argument("--mode",choices=["live","fixture"],default="live")
    ingest.add_argument("--fixture-clock")
    ingest.set_defaults(func=_ingest)
    for name in ("profile_cli",):
        try:
            module = importlib.import_module("ranking."+name)
        except ModuleNotFoundError as exc:
            if exc.name != "ranking."+name:
                raise
        else:
            module.register_parser(sub)
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except ContractError as exc:
        print(json.dumps({"error":exc.code,"message":str(exc),"issues":[x.to_dict() for x in exc.issues]},ensure_ascii=False),file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(json.dumps({"error":"invalid_input","message":str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
