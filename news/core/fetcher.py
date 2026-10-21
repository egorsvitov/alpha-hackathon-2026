"""HTTP-слой сбора: загрузка лент/страниц и извлечение текста статей.

Использует:
  - requests — HTTP-запросы;
  - feedparser — разбор RSS/Atom;
  - trafilatura — извлечение основного текста и метаданных из HTML;
  - hashlib + time — дедупликация и повторное использование скачанного.

Ключевые принципы:
  - сетевой доступ есть ТОЛЬКО в этом модуле, остальной код работает с файлами;
  - скачанные страницы кэшируются на диск, чтобы не дёргать источники повторно
    (важно и для скорости, и для вежливости к источнику);
  - текст, который не удалось извлечь, честно остаётся пустым — не выдумываем.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

import feedparser
import requests
import trafilatura

from news.core.model import ArticleCandidate
from news.core.registry import SourceSpec

# Будем представляться браузером, чтобы некоторые издания не блокировали.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "AlfaNewsPipeline/0.1"
)


@dataclass
class FetchResult:
    """Результат одного сетевого шага: список кандидатов или одна статья."""

    candidates: list[ArticleCandidate] = field(default_factory=list)
    text: str = ""
    title: str = ""
    published_at: str | None = None
    error: str | None = None


class Fetcher:
    """Загрузчик лент и статей с дисковым кэшем."""

    def __init__(
        self,
        cache_dir: str | Path = "data/processed/news_cache",
        timeout: float = 20.0,
        delay: float = 0.5,
        session: requests.Session | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.delay = delay
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": _USER_AGENT})

    # ---------------------------------------------------------------- внутреннее

    @staticmethod
    def _url_hash(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]

    def _cache_path(self, kind: str, url: str) -> Path:
        return self.cache_dir / f"{kind}_{self._url_hash(url)}.html"

    def _load_from_cache(self, kind: str, url: str) -> str | None:
        p = self._cache_path(kind, url)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def _save_to_cache(self, kind: str, url: str, content: str) -> None:
        self._cache_path(kind, url).write_text(content, encoding="utf-8")

    def _http_get(self, url: str, kind: str) -> str:
        """GET с вежливой паузой. Возвращает HTML/XML строку."""
        cached = self._load_from_cache(kind, url)
        if cached is not None:
            return cached
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        content = resp.text
        self._save_to_cache(kind, url, content)
        time.sleep(self.delay)
        return content

    # ---------------------------------------------------------------- лента/листинг

    def fetch_candidates(self, spec: SourceSpec) -> list[ArticleCandidate]:
        """Собирает кандидатов из источника (RSS/JSON/HTML) согласно spec.kind.

        Отвечает только за список ссылок; текст берётся позже из URL.
        """
        try:
            if spec.kind == "rss":
                return self._from_rss(spec)
            if spec.kind == "json":
                return self._from_json(spec)
            if spec.kind == "html":
                return self._from_html(spec)
            raise ValueError(f"Неизвестный kind: {spec.kind}")
        except Exception as e:  # noqa: BLE001
            # Один сломанный источник не должен ронять весь проход.
            raise RuntimeError(f"Ошибка сбора источника {spec.name}: {e}") from e

    def _from_rss(self, spec: SourceSpec) -> list[ArticleCandidate]:
        content = self._http_get(spec.url, spec.kind)
        parsed = feedparser.parse(content)
        out: list[ArticleCandidate] = []
        for entry in parsed.entries[: spec.limit]:
            link = entry.get("link")
            if not link:
                continue
            out.append(
                ArticleCandidate(
                    url=link,
                    title=(entry.get("title") or "").strip() or link,
                    published_at=self._rss_time(entry),
                    summary=(entry.get("summary") or entry.get("description") or "").strip() or None,
                )
            )
        return out

    @staticmethod
    def _rss_time(entry) -> str | None:
        for key in ("published_parsed", "updated_parsed"):
            t = entry.get(key)
            if t:
                import calendar
                return f"{calendar.timegm(t)}"
        return None

    def _from_json(self, spec: SourceSpec) -> list[ArticleCandidate]:
        content = self._http_get(spec.url, spec.kind)
        import json
        data = json.loads(content)
        items = data if isinstance(data, list) else data.get("items", [])
        out: list[ArticleCandidate] = []
        for item in items[: spec.limit]:
            url = item.get("url") or item.get("link")
            if not url:
                continue
            out.append(
                ArticleCandidate(
                    url=url,
                    title=str(item.get("title") or url),
                    published_at=item.get("published_at") or item.get("date"),
                )
            )
        return out

    def _from_html(self, spec: SourceSpec) -> list[ArticleCandidate]:
        content = self._http_get(spec.url, spec.kind)
        import re
        # Грубый разбор ссылок на странице-листинге: абсолютные и относительные.
        base = spec.url
        links = re.findall(r'href=["\']([^"\']+)["\']', content)
        out: list[ArticleCandidate] = []
        seen = set()
        for link in links:
            url = link if link.startswith("http") else requests.compat.urljoin(base, link)
            if url in seen or url == base:
                continue
            seen.add(url)
            out.append(ArticleCandidate(url=url, title=url))
            if len(out) >= spec.limit:
                break
        return out

    # ---------------------------------------------------------------- текст статьи

    def fetch_article(self, candidate: ArticleCandidate) -> FetchResult:
        """Выкачивает и извлекает текст статьи по URL."""
        try:
            content = self._http_get(candidate.url, "article")
        except Exception as e:  # noqa: BLE001
            return FetchResult(error=f"download: {e}")

        extracted = trafilatura.extract(
            content,
            url=candidate.url,
            include_comments=False,
            include_tables=False,
            favor_recall=False,
        )
        if not extracted:
            return FetchResult(error="extract: пустой текст")

        # Дата/заголовок из метаданных, если лента их не дала.
        meta = trafilatura.metadata.extract_metadata(content)
        title = candidate.title
        if meta and meta.title and len(meta.title) > len(title):
            title = meta.title
        published_at = candidate.published_at
        if meta and meta.date and not published_at:
            published_at = meta.date

        return FetchResult(text=extracted.strip(), title=title, published_at=published_at)

    def close(self) -> None:
        self.session.close()