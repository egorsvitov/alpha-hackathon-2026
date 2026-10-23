"""Explicit snapshot commands; no network during plan, inspect or export."""

import argparse
import sqlite3
import json
from pathlib import Path
from .pipeline import (
    plan_run,
    extract_mentions,
    cluster_mentions,
    resume_run,
    retrieve_candidates,
    status,
)
from .output import export_run, evaluate_run
from .runtime import (
    OfflineProvider,
    ProviderResponse,
    OpenAIProvider,
    ProviderProfile,
    ExecutionLimits,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--corpus", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--document-ids")
    p.add_argument("--configuration")
    for name in ("extract", "cluster", "resume"):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True)
        p.add_argument("--max-calls", required=True, type=int)
        p.add_argument(
            "--responses", help="Offline JSON array of provider response bodies"
        )
        p.add_argument("--live", action="store_true")
        p.add_argument("--limits")
        p.add_argument("--journal")
        p.add_argument("--allow-partial", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("--run", required=True)
    p = sub.add_parser("retrieve")
    p.add_argument("--run", required=True)
    p.add_argument("--position", required=True, type=int)
    p = sub.add_parser("export")
    p.add_argument("--run", required=True)
    p.add_argument("--output", required=True)
    p = sub.add_parser("evaluate")
    p.add_argument("--run", required=True)
    p.add_argument("--gold", required=True)
    p = sub.add_parser("evaluate-retrieval")
    p.add_argument("--documents", required=True)
    p.add_argument("--gold", required=True)
    sub.add_parser("check")
    p = sub.add_parser("demo")
    p.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    def load(path):
        return json.loads(Path(path).read_text())

    try:
        if args.command == "plan":
            result = plan_run(
                args.corpus,
                args.output,
                load(args.document_ids) if args.document_ids else None,
                load(args.configuration) if args.configuration else None,
            )
            result = {
                k: result[k]
                for k in ("schema_version", "corpus_id", "run_id", "configuration")
            }
        elif args.command == "status":
            result = status(args.run)
        elif args.command == "retrieve":
            result = retrieve_candidates(args.run, args.position)
        elif args.command == "export":
            result = export_run(args.run, args.output)
        elif args.command == "evaluate":
            result = evaluate_run(args.run, args.gold)
        elif args.command == "evaluate-retrieval":
            from .evaluation import evaluate_gold_retrieval

            result = evaluate_gold_retrieval(
                [
                    json.loads(line)
                    for line in Path(args.documents).read_text().splitlines()
                    if line.strip()
                ],
                load(args.gold),
            )
        elif args.command == "check":
            from .checks import check

            result = check()
        elif args.command == "demo":
            from .demo import demo

            result = demo(args.output)
        else:
            if args.live and args.responses:
                raise ValueError("choose_live_or_offline")
            stage = (
                "verification"
                if args.command == "cluster"
                or args.command == "resume"
                and status(args.run)["phase"] != "extracting"
                else "extraction"
            )
            profile = ProviderProfile.model_validate(
                load(Path(args.run) / "plan.json")["configuration"][stage + "_profile"]
            )
            if args.live:
                if not args.limits:
                    raise ValueError("live_limits_required")
                from llm.events.extraction.admin_cli import load_cli_env

                load_cli_env()
                provider = OpenAIProvider(profile)
            else:
                if not args.responses:
                    raise ValueError("offline_responses_required")
                provider = OfflineProvider(
                    [ProviderResponse(body=b) for b in load(args.responses)]
                )
            limits = (
                ExecutionLimits.model_validate(load(args.limits))
                if args.limits
                else None
            )
            kw = {"limits": limits, "journal": args.journal}
            if stage == "verification":
                kw["allow_partial"] = args.allow_partial
            result = {
                "extract": extract_mentions,
                "cluster": cluster_mentions,
                "resume": resume_run,
            }[args.command](args.run, provider, args.max_calls, **kw)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "code": getattr(exc, "code", "pipeline_error"),
                    "path": "/",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
