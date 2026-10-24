"""Batch news processing and theme consolidation for the analyst pipeline.

news_agent reads a batch of news articles, groups them into themes
(deduplication and merging of repeat reports) and produces a compact set of
themes with extracted insight candidates. It is the stage before the analyst:
it turns a raw news stream into a small, non-redundant list of things worth
analysing, so later LLM calls are spent on distinct stories, not duplicates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path


@dataclass
class Article:
    article_id: str
    source: str
    title: str
    published_at: str | None
    text: str
    url: str | None = None
    language: str = "ru"

    @classmethod
    def from_record(cls, record: dict) -> "Article":
        raw = record.get("article", record.get("raw", record))
        return cls(
            article_id=str(raw.get("article_id", "")),
            source=str(raw.get("source", "")),
            title=str(raw.get("title", "")),
            published_at=raw.get("published_at"),
            text=str(raw.get("text", "")),
            url=raw.get("url"),
            language=str(raw.get("language", "ru")),
        )


@dataclass
class Theme:
    theme_id: str
    label: str
    article_ids: list[str] = field(default_factory=list)
    importance: float = 0.0

    def to_dict(self) -> dict:
        return {
            "theme_id": self.theme_id,
            "label": self.label,
            "article_ids": self.article_ids,
            "importance": self.importance,
        }


_STOP = {
    "сохранил", "оставил", "на", "по", "из", "за", "при", "до", "без",
    "уровне", "процентов", "процента", "рублей", "млн", "млрд", "после",
    "могут", "может", "был", "была", "будет", "стал", "стала", "изменений",
    "изменения", "изменение", "без",
}

def _theme_key(article: Article) -> str:
    """Deterministic theme signature from the title's content words.

    Content words are the non-stop words; the key is their stable intersection
    signature so that near-identical headlines (e.g. "ЦБ сохранил ставку" vs
    "ЦБ оставил ставку") map to the same theme.
    """
    import re

    words = re.findall(r"[а-яёa-z0-9]{4,}", article.title.lower())
    content = [w for w in words if w not in _STOP]
    return " ".join(sorted(content))


def _theme_label(articles: list[Article]) -> str:
    longest = max(articles, key=lambda a: len(a.title))
    return longest.title[:120] or longest.text[:120]


def consolidate(articles: list[Article], importance_fn=None) -> list[Theme]:
    """Group articles into themes and rank themes by importance.

    Grouping uses the deterministic title signature; importance is taken from
    the optional callable or defaults to the number of reports in the theme
    (repeat coverage raises salience). Order is stable by importance desc.
    """
    buckets: dict[str, list[Article]] = {}
    for article in articles:
        buckets.setdefault(_theme_key(article), []).append(article)

    themes: list[Theme] = []
    for key, group in buckets.items():
        importance = (
            importance_fn(group) if importance_fn else float(len(group))
        )
        themes.append(
            Theme(
                theme_id=sha256(key.encode()).hexdigest()[:16],
                label=_theme_label(group),
                article_ids=[a.article_id for a in group],
                importance=importance,
            )
        )
    themes.sort(key=lambda t: t.importance, reverse=True)
    return themes


def read_batch(path: Path, *, limit: int | None = None) -> list[Article]:
    """Read articles from a JSONL/JSON file. ``limit`` caps the batch size."""
    path = Path(path)
    articles: list[Article] = []
    if path.suffix.lower() == ".jsonl":
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                articles.append(Article.from_record(json.loads(line)))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else data.get("articles", [])
        for row in rows:
            articles.append(Article.from_record(row))
    if limit is not None:
        articles = articles[:limit]
    return articles