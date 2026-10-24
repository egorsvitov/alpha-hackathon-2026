import pytest

from llm.context_agent import HistoryEntry, classify, classify_batch


def test_new_theme_label():
    assert classify("a,b", []) == "new"


def test_repeated_theme_becomes_updated():
    history = [HistoryEntry(theme_id="t1", key="a,b", article_count=1)]
    assert classify("a,b", history) == "updated"


def test_classify_batch_accepts_dicts():
    themes = [{"theme_id": "t1", "article_ids": ["a", "b"]}]
    result = classify_batch(themes, [])
    assert result[0]["novelty"] == "new"