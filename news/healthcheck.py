"""Fail on absent, failed or stale collection; no unconditional healthy result."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def healthy(state, *, every, current=None, timeout=3600):
    current = current or datetime.now(timezone.utc)
    run = state.get('last_run') or {}
    if not run.get('ok') or run.get('collection',{}).get('status') not in ('collected','no_new'):
        return False
    try:
        completed = datetime.fromisoformat(run['completed_at'])
        return completed.tzinfo is not None and 0 <= (current-completed).total_seconds() <= every+timeout+120
    except (KeyError,ValueError):
        return False


if __name__ == '__main__':
    try:
        state = json.loads(Path(os.environ.get('NEWS_STATE','/data/news_state/schedule_state.json')).read_text())
        ok = healthy(state,every=int(os.environ.get('NEWS_EVERY','3600')))
    except (OSError,ValueError):
        ok = False
    raise SystemExit(0 if ok else 1)
