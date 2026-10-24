import pytest

from llm.news_agent import Article, consolidate


def _article(aid, title):
    return Article(article_id=aid, source="s", title=title, published_at=None, text=title)


def test_near_identical_headlines_merge_into_one_theme():
    a1 = _article("a1", "ЦБ сохранил ключевую ставку на уровне 14 процентов")
    a2 = _article("a2", "ЦБ оставил ключевую ставку без изменений на 14 процентов")
    a3 = _article("a3", "Металлурги нарастили экспорт стали в Китай")
    themes = consolidate([a1, a2, a3])
    assert len(themes) == 2
    merged = next(t for t in themes if t.article_ids == ["a1", "a2"])
    assert merged.importance == 2.0


def test_importance_ranks_repeat_coverage_first():
    a1 = _article("a1", "ЦБ сохранил ключевую ставку")
    a2 = _article("a2", "ЦБ оставил ключевую ставку без изменений")
    a3 = _article("a3", "Металлурги нарастили экспорт стали в Китай")
    themes = consolidate([a3, a1, a2])
    assert themes[0].article_ids == ["a1", "a2"]