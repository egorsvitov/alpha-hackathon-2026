"""Генерация стабильных ID статей и приведения времени.

ID статьи: source-префикс + короткий хэш URL — детерминированно, поэтому
повторный проход по той же ссылке даёт тот же article_id (удобно для
дедупликации и воспроизводимости).
"""
from __future__ import annotations

import hashlib


def stable_id(source: str, url: str) -> str:
    """Непрозрачный стабильный ID: <source>:<sha1(url)[:12]>."""
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return f"{source}:{h}"


def normalize_time(value: str | None) -> str | None:
    """Приводит время к ISO-8601; невалидное значение честно возвращает None.

    Парсер не выдумывает время публикации — если источник не дал корректной
    даты, остаётся null (согласовано с контрактом B6: null, а не пустая строка).
    """
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    # Уже ISO без TZ или с TZ — оставляем как есть, если похоже на дату.
    if "T" in v or (len(v) >= 10 and v[4] == "-"):
        return v
    # Unix-секунды (из feedparser).
    try:
        import datetime as _dt
        ts = float(v)
        return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None