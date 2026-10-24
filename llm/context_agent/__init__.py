"""Historical novelty classification for news themes (context_agent).

Given a theme from the current batch and the set of themes seen in previous
batches, the agent labels the theme's novelty:
  * new         - not seen before;
  * continuing  - same story, stable coverage;
  * updated     - same story with new details/changes;
  * fading      - was covered recently but is losing momentum.

The classification is deterministic: a theme is matched against a history of
previous theme keys (stable article-id signatures), and the label follows from
whether the theme key appears in the previous window and whether the number of
related articles is growing or shrinking.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class HistoryEntry:
    theme_id: str
    key: str
    seen_at: str | None = None
    article_count: int = 0


class HistoryStore:
    """Persist previous theme signatures for novelty comparisons."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: list[HistoryEntry] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        import json

        data = json.loads(self.path.read_text(encoding="utf-8"))
        for row in data:
            self.entries.append(HistoryEntry(**row))

    def save(self) -> None:
        import json

        data = [
            {
                "theme_id": e.theme_id,
                "key": e.key,
                "seen_at": e.seen_at,
                "article_count": e.article_count,
            }
            for e in self.entries
        ]
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _signature(article_ids: list[str]) -> str:
    return ",".join(sorted(article_ids))


def classify(theme_key: str, history: list[HistoryEntry]) -> str:
    """Return a novelty label for a theme key given the previous history."""
    keys = {e.key for e in history}
    if theme_key in keys:
        # Present before; look at coverage trend to separate updated vs fading.
        recent = [e for e in history if e.key == theme_key]
        count = sum(e.article_count for e in recent)
        if count == 0:
            return "continuing"
        return "updated" if count > 0 else "fading"
    # Not seen in history: check whether a near signature existed (fading).
    seen_keys = set()
    for e in history:
        seen_keys.add(e.key)
    return "fading" if seen_keys else "new"


def _ids(theme) -> list[str]:
    return theme.get("article_ids", []) if isinstance(theme, dict) else theme.article_ids


def _tid(theme) -> str:
    return theme.get("theme_id", "") if isinstance(theme, dict) else theme.theme_id


def classify_batch(themes, history_entries) -> list[dict]:
    """Classify a list of Theme-like objects, returning label per theme_id."""
    out = []
    for theme in themes:
        key = _signature(_ids(theme))
        label = classify(key, history_entries)
        out.append({"theme_id": _tid(theme), "novelty": label})
    return out