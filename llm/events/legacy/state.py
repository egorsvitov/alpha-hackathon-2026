"""B2. Приём батчей и состояние: неизменяемые версии, хэши, журнал.

Отвечает за:
  - повторный запуск без потери истории;
  - обработку исправлений публикаций (новая версия под прежним ID);
  - продолжение прерванного батча;
  - журнал обработки.

Принципы из плана B2:
  - текст статьи неизменяем; изменение под прежним ID создаёт новую версию;
  - цитаты и координаты всегда ссылаются на конкретную версию;
  - каждая статья обрабатывается один раз (по хэшу содержимого), чтобы не
    жечь бюджет LLM на повторные извлечения.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from news.core.model import Article

# Версия конфигурации извлечения — меняется при изменении промпта/схемы.
# Любое изменение здесь заставляет переизвлечь статьи (происхождение сохраняется).
EXTRACTOR_VERSION = "events-v0.1"


def content_hash(text: str) -> str:
    """SHA-256 содержимого статьи — ключ неизменяемой версии."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class ArticleVersion:
    """Одна неизменяемая версия статьи."""

    article_id: str
    version: int
    content_hash: str
    extracted: bool
    extracted_at: str | None = None
    extractor_version: str | None = None
    claims_count: int = 0


@dataclass
class BatchResult:
    """Итог обработки батча (см. B2)."""

    new_articles: int = 0
    updated_articles: int = 0
    skipped_unchanged: int = 0
    events_created: int = 0
    events_updated: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    budget_pending: list[str] = field(default_factory=list)
    llm_calls: int = 0
    cost_rub: float = 0.0

    def to_dict(self) -> dict:
        return {
            "new_articles": self.new_articles,
            "updated_articles": self.updated_articles,
            "skipped_unchanged": self.skipped_unchanged,
            "events_created": self.events_created,
            "events_updated": self.events_updated,
            "errors": self.errors,
            "budget_pending": self.budget_pending,
            "llm_calls": self.llm_calls,
            "cost_rub": round(self.cost_rub, 4),
        }


class StateStore:
    """Дисковое состояние батчей: версии статей + журнал.

    Формат файла state.json:
      {"articles": {"<article_id>": ArticleVersion}, "journal": [...]}
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self._state: dict = {"articles": {}, "journal": []}
        self._load()

    def _load(self) -> None:
        if self.state_file.exists():
            try:
                self._state = json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self._state = {"articles": {}, "journal": []}

    def _save(self) -> None:
        tmp = self.state_file.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    # ------------------------------------------------------------ артиклы

    def known_articles(self) -> dict[str, ArticleVersion]:
        """Возвращает {article_id: ArticleVersion} для уже известных версий."""
        out: dict[str, ArticleVersion] = {}
        for aid, v in self._state["articles"].items():
            out[aid] = ArticleVersion(**v)
        return out

    def version_for(self, article: Article) -> ArticleVersion | None:
        """Возвращает известную версию статьи или None, если такой нет."""
        known = self._state["articles"].get(article.article_id)
        if not known:
            return None
        return ArticleVersion(**known)

    def already_extracted(self, article: Article, h: str | None = None) -> bool:
        """Статья уже извлечена с тем же содержимым и конфигом извлечения?"""
        ver = self.version_for(article)
        if not ver or not ver.extracted:
            return False
        return (h or content_hash(article.text)) == ver.content_hash and ver.extractor_version == EXTRACTOR_VERSION

    def mark_extracted(self, article: Article, claims_count: int) -> None:
        """Отмечает версию статьи как извлечённую (с подсчётом утверждений)."""
        h = content_hash(article.text)
        ver = self.version_for(article)
        new_version = 1
        if ver:
            # Новое содержимое под прежним ID = новая версия; иначе сохраняем версию.
            new_version = ver.version + 1 if ver.content_hash != h else ver.version
        self._state["articles"][article.article_id] = {
            "article_id": article.article_id,
            "version": new_version,
            "content_hash": h,
            "extracted": True,
            "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "extractor_version": EXTRACTOR_VERSION,
            "claims_count": claims_count,
        }
        self._save()

    # ------------------------------------------------------------ журнал

    def journal_append(self, entry: dict) -> None:
        self._state["journal"].append({
            **entry,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self._save()

    def journal(self) -> list[dict]:
        return list(self._state.get("journal", []))

    # ------------------------------------------------------------ дедуп по батчу

    def pending_articles(self, articles: list[Article]) -> list[Article]:
        """Возвращает статьи батча, которые нужно извлечь (не извлечены или изменены).

        Пропускает уже извлечённые с тем же содержимым и конфигом.
        """
        pending: list[Article] = []
        for a in articles:
            if self.already_extracted(a):
                continue
            pending.append(a)
        return pending
