"""Сохранение статей в JSONL на диск.

Каждая строка — одна Article в формате команды (см. news/core/model.py).
Файл-накопитель и файл-снимок за проход; дедупликация по URL в рамках файла.
"""
from __future__ import annotations

from pathlib import Path

from news.core.model import Article

# Имя файла-накопителя всего корпуса (переживает перезапуски).
CORPUS_FILENAME = "news_corpus.jsonl"


class JsonlStore:
    """Пишет статьи в JSONL и умеет перезаписывать накопитель из набора статей.

    Принцип: парсер собирает статьи в память, затем store перезаписывает
    накопитель атомарно (tmp + rename), поэтому не бывает битых половин.
    """

    def __init__(self, out_dir: str | Path) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    @property
    def corpus_path(self) -> Path:
        return self.out_dir / CORPUS_FILENAME

    def read_existing(self) -> dict[str, Article]:
        """Читает текущий накопитель в словарь URL -> Article."""
        out: dict[str, Article] = {}
        p = self.corpus_path
        if not p.exists():
            return out
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                art = Article.model_validate_json(line)
            except Exception:  # noqa: BLE001
                continue
            if art.url:
                out[art.url] = art
        return out

    def merge(self, articles: list[Article], dedupe_by: str = "url") -> dict[str, Article]:
        """Соединяет существующие статьи с новыми; новые перекрывают по ключу."""
        existing = self.read_existing()
        key = lambda a: (a.url or a.article_id) if dedupe_by == "url" else a.article_id  # noqa: E731
        merged = {key(a): a for a in existing.values()}
        for a in articles:
            merged[key(a)] = a
        return merged

    def write(self, articles: list[Article]) -> Path:
        """Перезаписывает накопитель переданным списком статей (атомарно)."""
        tmp = self.out_dir / ".corpus.tmp.jsonl"
        with tmp.open("w", encoding="utf-8") as f:
            for a in articles:
                f.write(a.to_jsonl() + "\n")
        tmp.replace(self.corpus_path)
        return self.corpus_path