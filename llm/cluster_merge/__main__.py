"""Explicit CLI; no network during schema generation, inspection or planning."""

import argparse
import json
from pathlib import Path

from . import models
from .core import plan_merge
from .models import MergeRequest
from .provider import OpenAIProvider, SQLiteQuota
from .service import MergeService


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["plan", "run", "resume", "get", "schemas"])
    p.add_argument(
        "input", help="request JSON, operation/result ID, or schema output directory"
    )
    p.add_argument("--db", default="data/processed/cluster_merge/state.sqlite3")
    p.add_argument("--max-calls", type=int)
    p.add_argument("--quota-db")
    p.add_argument("--quota-limit", type=int)
    args = p.parse_args()
    if args.command == "schemas":
        path = Path(args.input)
        path.mkdir(parents=True, exist_ok=True)
        for name in [
            "MergeRequest",
            "MergeResult",
            "MergePlan",
            "Answer",
            "ValidationReport",
        ]:
            (path / (name + ".json")).write_text(
                json.dumps(
                    getattr(models, name).model_json_schema(),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        return
    if args.command == "plan":
        value = plan_merge(
            MergeRequest.model_validate_json(Path(args.input).read_text())
        )
    else:
        if args.command in ("run", "resume"):
            if args.max_calls is None or not args.quota_db or not args.quota_limit:
                p.error(
                    "network commands require --max-calls, --quota-db and --quota-limit"
                )
            # .env is a CLI concern; SDK and library never load it implicitly.
            import os

            env = Path(".env")
            if env.exists():
                for line in env.read_text().splitlines():
                    if line.startswith("LLM_API_KEY="):
                        os.environ.setdefault(
                            "LLM_API_KEY", line.split("=", 1)[1].strip().strip("\"'")
                        )
            limiter = SQLiteQuota(args.quota_db, args.quota_limit)
        else:
            limiter = None
        service = MergeService(OpenAIProvider(), limiter, args.db)
        if args.command == "get":
            value = service.get_merge_result(args.input)
        elif args.command == "resume":
            value = service.resume_merge(args.input, max_calls=args.max_calls)
        else:
            value = service.merge_cluster(
                MergeRequest.model_validate_json(Path(args.input).read_text()),
                max_calls=args.max_calls,
            )
    print(value.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
