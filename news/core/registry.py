"""Описание источника новостей и реестр источников.

Реестр — это единственное место, где определяется, какие источники участвуют
в сборе: код парсеров получает из реестра только список URL и настройки.
Это делает добавление/отключение источника правкой конфига, а не кода.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Тип источника. category (группа для фильтрации) и kind (способ сбора).
SourceCategory = Literal[
    "macro",        # макроэкономика, финансы, регулятор
    "industry",     # отраслевые издания и ассоциации
    "business",     # деловые и общеэкономические СМИ
    "official",     # госорганы, регуляторы, статистика
]
SourceKind = Literal["rss", "json", "html"]


class SourceSpec(BaseModel):
    """Конфигурация одного источника в реестре.

    kind определяет способ сбора:
      - "rss"   — лента RSS/Atom (feedparser);
      - "json"  — JSON-эндпоинт (например, API, где можно взять список ссылок);
      - "html"  — страница-листинг с HTML-ссылками.
    Для "rss" url указывает на ленту; для "html" — на страницу-листинг.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kind: SourceKind
    url: str
    category: SourceCategory = "business"
    # Лимит записей за один проход. Для лент обычно достаточно 50–100.
    limit: int = Field(default=100, ge=1)
    # Язык по умолчанию для статей этого источника.
    language: str = "ru"
    # Если true — брать только заголовки/ссылки без полного текста.
    headlines_only: bool = False


class SourceRegistry(BaseModel):
    """Реестр источников. Собирается из sources.json + inline-переопределений."""

    sources: list[SourceSpec] = Field(min_length=0)

    def enabled(self, categories: list[str] | None = None) -> list[SourceSpec]:
        return [
            s for s in self.sources
            if not categories or s.category in categories
        ]


def load_registry(path: str | Path = "news/sources.json") -> SourceRegistry:
    """Загружает реестр из JSON и возвращает валидную модель.

    raise: ValueError, если файл отсутствует или не проходит валидацию.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"Реестр источников не найден: {path}")
    try:
        import json
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Некорректный JSON реестра {path}: {e}") from e
    registry = SourceRegistry.model_validate(data)
    names = [s.name for s in registry.sources]
    if len(names) != len(set(names)):
        raise ValueError("В реестре источников есть дубликаты имён")
    return registry