"""Адаптер статей news/ → строгий ArticleInput модуля llm/events.

Сборщик news/ (news/core/model.py: Article) хранит упрощённую модель без полей
происхождения, которые требуются ArticleInput (content_kind,
is_full_original_text, is_synthetic). Этот адаптер заполняет их разумными
значениями по умолчанию, сохраняя все поля, которые уже есть.

Важно:
  - не выдумывает данные; отсутствующие поля остаются None;
  - content_kind="original", т.к. сборщик берёт полный текст оригинала;
  - is_synthetic=False — реальные собранные статьи, не синтетика.
"""
from __future__ import annotations

from typing import Any

from news.core.model import Article


def to_article_input_dict(article: Article) -> dict[str, Any]:
    """Преобразует нашу Article в dict, валидный для ArticleInput.

    Если text пуст, возвращает None-подобную запись с пометкой, что текст
    отсутствует (B2 отклонит как неполную? нет — ArticleInput требует text).
    Пустой текст оставляем как есть; решение о пригодности принимает B2/B4.
    """
    return {
        "article_id": article.article_id,
        "source": article.source,
        "url": article.url,
        "title": article.title,
        "published_at": article.published_at,
        "language": article.language,
        "text": article.text,
        "content_kind": "original",
        "is_full_original_text": bool(article.text),
        "is_synthetic": False,
    }


def load_news_corpus(jsonl_path: str) -> list[dict[str, Any]]:
    """Читает JSONL-накопитель news/ и возвращает список dict для import-batch.

    Пропускает статьи с пустым text (для них извлечение не имеет смысла),
    но не отбрасывает остальные поля. Возвращает только пригодные записи.
    """
    from pathlib import Path

    p = Path(jsonl_path)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            article = Article.model_validate_json(line)
        except Exception:  # noqa: BLE001
            continue
        if not article.text:
            continue  # пустой текст бесполезен для извлечения
        out.append(to_article_input_dict(article))
    return out