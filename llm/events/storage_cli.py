"""B2 archive and queue administration. Does not execute processing jobs."""

import argparse
import json
from pathlib import Path

from pydantic import BaseModel

from .storage import EventStore
from .validation import ContractValidationError


def _jsonable(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="SQLite path (default: data/processed/events/state.sqlite3)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("import-batch", help="Import a UTF-8 JSON array of articles")
    p.add_argument("input", type=Path)
    for command in ("resume-batch", "show-batch"):
        p = sub.add_parser(command)
        p.add_argument("batch_id")
    p = sub.add_parser("list-batches", help="Find receipts, including interrupted imports")
    p.add_argument("--status", choices=["importing", "completed", "partial", "rejected"])
    p = sub.add_parser("get-article")
    p.add_argument("source_id")
    p = sub.add_parser("current-article")
    p.add_argument("article_id")
    p = sub.add_parser("list-versions")
    p.add_argument("article_id")
    p = sub.add_parser("restore-version")
    p.add_argument("article_id")
    p.add_argument("version", type=int)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("enqueue")
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--source-id")
    selection.add_argument("--batch")
    p.add_argument("--spec", type=Path, required=True)
    for command in ("show-job", "list-attempts", "retry-job"):
        p = sub.add_parser(command)
        p.add_argument("job_id")
    p = sub.add_parser("show-artifact")
    p.add_argument("artifact_id")
    sub.add_parser("requeue-expired")
    args = parser.parse_args(argv)
    try:
        store = EventStore(args.db)
        code = 0
        if args.command == "import-batch":
            result = store.import_batch(args.input)
        elif args.command == "list-batches":
            result = store.list_batches(import_status=args.status)
        elif args.command == "resume-batch":
            result = store.resume_batch(args.batch_id)
        elif args.command == "show-batch":
            result = store.get_batch(args.batch_id)
        elif args.command == "get-article":
            result = store.get_article(args.source_id)
        elif args.command == "current-article":
            result = store.get_current_article(args.article_id)
        elif args.command == "list-versions":
            result = store.list_versions(args.article_id)
        elif args.command == "restore-version":
            result = store.restore_version(args.article_id, args.version, args.reason)
        elif args.command == "enqueue":
            spec = store._spec(json.loads(args.spec.read_text()))
            if args.source_id:
                result = store.enqueue(args.source_id, spec)
            else:
                receipt = store.get_batch(args.batch)
                jobs, skipped, seen = [], [], set()
                for item in receipt.items:
                    if (item.disposition not in {"new", "updated", "unchanged"}
                            or store.get_current_article(item.article_id).source_id != item.source_id):
                        skipped.append(item.index)
                    elif item.source_id not in seen:
                        jobs.append(store.enqueue(item.source_id, spec))
                        seen.add(item.source_id)
                result = {"batch_id": args.batch, "jobs": jobs, "skipped_indices": skipped}
        elif args.command == "show-job":
            result = store.get_job(args.job_id)
        elif args.command == "list-attempts":
            result = store.list_attempts(args.job_id)
        elif args.command == "retry-job":
            result = store.retry_job(args.job_id)
        elif args.command == "show-artifact":
            result = store.get_artifact(args.artifact_id)
        else:
            result = store.requeue_expired()
        if args.command in {"import-batch", "resume-batch"} and result.import_status in {"partial", "rejected"}:
            code = 1
        print(json.dumps(_jsonable(result), ensure_ascii=False, indent=2))
        return code
    except ContractValidationError as exc:
        print(json.dumps({"status": "error", "issues": [x.model_dump() for x in exc.issues]}, ensure_ascii=False, indent=2))
        return 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "issues": [{"code": "invalid_input", "path": "/", "message": str(exc)}]}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
