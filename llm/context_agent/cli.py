"""CLI for context_agent: classify theme novelty against a persisted history."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import HistoryEntry, HistoryStore, classify_batch


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m llm.context_agent.cli")
    parser.add_argument("--themes", required=True, help="JSON/JSONL of themes (theme_id + article_ids)")
    parser.add_argument("--history", required=True, help="History store path")
    args = parser.parse_args(argv)

    store = HistoryStore(Path(args.history))
    path = Path(args.themes)
    if path.suffix.lower() == ".jsonl":
        themes = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        themes = data if isinstance(data, list) else data.get("themes", [])

    results = classify_batch(themes, store.entries)
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    # Persist current themes as history for the next run.
    for theme in themes:
        store.entries.append(HistoryEntry(
            theme_id=theme["theme_id"],
            key=",".join(sorted(theme.get("article_ids", []))),
            article_count=len(theme.get("article_ids", [])),
        ))
    store.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())