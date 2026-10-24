"""QA smoke for the retriever: runs four checks on synthetic data and prints a report.

Run from repo root:  python -m retriever.qa_smoke

The checks cover (1) quality metrics on a labeled pool, (2) integration with the
news JSONL format, (3) interest generation from the OKVED catalogue, and
(4) boundary/stress behaviour. Everything uses local synthetic inputs, so no
network, model weights, GPU or bank data are required.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from retriever.catalogues.activity_interests import load_activity_interests
from retriever.evaluation import evaluate_batch, make_review_pool, read_batch
from retriever.example import run_example
from retriever.importers import read_articles
from retriever.models import CompanyInterestSet, Interest

RESULTS = []


def check(name, fn):
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 - report every failure with its message
        RESULTS.append((name, "FAIL", f"{type(exc).__name__}: {exc}"))
        return
    RESULTS.append((name, "OK", detail))


def _qa1():
    """Quality metrics on a manually labeled pool."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "demo"
        run_example(path)
        batch = read_batch(path / "batch.json")
        pool = make_review_pool([path / "batch.json"])
        # Label everything relevant so the metrics are exercised, not skipped.
        for row in pool["cases"]:
            row["relevance"] = "relevant"
            row["story_id"] = "story-" + row["case_id"][:8]
        report = evaluate_batch(batch, pool, top_k=2)
        return f"returned={report['returned']} precision={report['judged_precision']} recall={report['known_positive_pool_recall']}"


def _qa2():
    """Integration with the news JSONL format (article_id/text/published_at)."""
    records = [
        {"article": {"article_id": "n1", "text": "Рост цен на топливо в сентябре ускорился.", "published_at": "2026-10-01T10:00:00+03:00"}},
        {"article": {"article_id": "n2", "text": "Регулятор сохранил ключевую ставку.", "published_at": "2026-10-02T10:00:00+03:00"}},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "news.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n")
        articles, diagnostics = read_articles(path)
        return f"parsed={len(articles)} skipped={len(diagnostics)} ids={[a.article_id for a in articles]}"


def _qa3():
    """Interest generation from the OKVED activity catalogue."""
    interests = load_activity_interests("01.11.1")
    if not interests:
        return "no interests for okved 01.11.1"
    return f"okved=01.11.1 interests={len(interests)} first={interests[0]['title']}"


def _qa4():
    """Boundary: only inactive interests return no hits instead of raising."""
    from retriever.models import IndexSnapshot, SearchItem, VectorRecord, text_hash
    from retriever.search import exact_search

    interest = Interest(interest_id="off", text="inactive", vector=[1.0, 0.0], active=False)
    profile = CompanyInterestSet(
        company_id="c", profile_version="1", interest_set_version="1",
        encoder_fingerprint="fp", dimension=2, interests=[interest],
    )
    item = SearchItem(item_id="a", text="whole source text", embedding_text="whole source text",
                      source_revision_ids=["a"])
    vector = VectorRecord(vector_id="a", item_id="a", embedding_text_sha256=text_hash("whole source text"),
                          values=[1.0, 0.0])
    snapshot = IndexSnapshot(corpus_id="corpus", generation=1, encoder_fingerprint="fp", dimension=2,
                             items=[item], vectors=[vector])
    hits = exact_search(snapshot, profile)
    return f"inactive_interests -> hits={len(hits)}"


if __name__ == "__main__":
    check("1. quality-metrics", _qa1)
    check("2. news-jsonl-integration", _qa2)
    check("3. interest-catalogue", _qa3)
    check("4. boundary-empty-interests", _qa4)
    print("=== retriever QA smoke ===")
    for name, status, detail in RESULTS:
        print(f"[{status}] {name}: {detail}")
    if any(s == "FAIL" for _, s, _ in RESULTS):
        raise SystemExit(1)