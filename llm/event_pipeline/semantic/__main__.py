"""Explicit stages: plan -> embed (resumable) -> compare. No chat API calls."""

import argparse
import json
import sys
from .experiment import plan, embed, compare, read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--corpus", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--document-ids")
    p.add_argument("--max-chars", type=int, default=2000)
    p = sub.add_parser("embed")
    p.add_argument("--run", required=True)
    p.add_argument("--model", default="BAAI/bge-m3")
    p.add_argument(
        "--revision", required=True, help="Immutable 40-character model commit SHA"
    )
    p.add_argument("--device", default="cpu")
    p.add_argument("--allow-download", action="store_true")
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--max-batches", type=int, required=True)
    p = sub.add_parser("compare")
    p.add_argument("--run", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--threshold", type=float, default=0.75)
    p.add_argument("--alpha", type=float, default=0.4)
    p.add_argument("--half-life-days", type=float, default=7)
    p.add_argument("--backend", choices=("reference", "scipy"), default="scipy")
    p.add_argument("--max-units", type=int, default=5000)
    p.add_argument("--max-matrix-mb", type=int, default=512)
    p.add_argument("--neighbors", type=int, default=10)
    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        if command == "plan":
            if args["document_ids"]:
                args["document_ids"] = read(args["document_ids"])
            result = plan(**args)
        elif command == "embed":
            from .encoder import SentenceEncoder

            root = args.pop("run")
            batch_size, max_batches = args.pop("batch_size"), args.pop("max_batches")
            encoder = SentenceEncoder(**args)
            result = embed(
                root, encoder, batch_size=batch_size, max_batches=max_batches
            )
        else:
            root = args.pop("run")
            result = compare(root, **args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, ImportError) as error:
        print(
            json.dumps(
                {
                    "code": "semantic_experiment_error",
                    "path": command,
                    "message": str(error),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
