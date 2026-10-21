"""Модель сырой новостной статьи — универсальный контракт входа парсера.

Формат совпадает с договорённостью команды (формат Егора):
article_id / source / url / title / published_at / language / text.

Это промежуточный, независящий от компании формат. Дальше NLP-модуль (пункт 1)
превращает эти статьи в EventBundle по docs/contracts/event-bundle.schema.json.
Парсер НЕ заполняет поля B6 — он только собирает сырьё и метаданные.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Язык по умолчанию для корпуса.
DEFAULT_LANGUAGE = "ru"


class Article(BaseModel):
    """Одна собранная статья в формате команды.

    article_id — стабильный непрозрачный ID (source-префикс + счётчик/хэш).
    url и published_at могут быть неизвестны (null) — это честное отсутствие,
    а не пустая строка. text может быть пустым, если выделить текст не удалось.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    article_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    url: str | None = None
    title: str = Field(min_length=1)
    published_at: str | None = None
    language: str = DEFAULT_LANGUAGE
    text: str = ""

    def to_jsonl(self) -> str:
        return self.model_dump_json(by_alias=True)


class ArticleCandidate(BaseModel):
    """Статья до извлечения текста (из RSS/системного листинга).

    Здесь хранятся только те данные, которые даёт лента: ссылка, заголовок,
    дата публикации и, опционально, краткое описание. Текст извлекается
    отдельным шагом из URL. Позволяет разделить «нашёл ссылку» и «выкачал текст».
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    title: str
    published_at: str | None = None
    summary: str | None = None