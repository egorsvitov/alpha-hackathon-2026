"""CLI for news_agent: consolidate a batch of articles into themes."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import consolidate, read_batch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m llm.news_agent.cli")
    parser.add_argument("--articles", required=True, help="JSONL/JSON batch of articles")
    parser.add_argument("--limit", type=int, help="Cap batch size (default: no cap)")
    parser.add_argument("--output", help="Output JSONL path (default: stdout)")
    args = parser.parse_args(argv)

    articles = read_batch(Path(args.articles), limit=args.limit)
    themes = consolidate(articles)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with Path(args.output).open("w", encoding="utf-8") as stream:
            for theme in themes:
                stream.write(json.dumps(theme.to_dict(), ensure_ascii=False) + "\n")
    else:
        for theme in themes:
            print(json.dumps(theme.to_dict(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())