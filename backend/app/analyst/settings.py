"""Настройки модуля «LLM Business Analyst».

Всё читается из переменных окружения (эксперты банка подставляют свой ключ).
Файл ``backend/.env`` подгружается через python-dotenv только при чтении из
``os.environ`` и никогда не перезаписывает уже заданные переменные.

Переменные:

* ``ANALYST_MODE`` — ``template`` (по умолчанию, прежние шаблоны без LLM),
  ``llm`` (аналитик через шлюз LLM, кэш и офлайн-откат), ``cached``
  (только сохранённые ответы, сеть не используется).
* ``LLM_API_KEY`` — ключ шлюза организаторов (секрет, нигде не логируется).
* ``LLM_BASE_URL`` — адрес OpenAI-совместимого API, по умолчанию российский шлюз.
* ``LLM_FALLBACK_BASE_URL`` — запасной адрес (пусто — без запасного).
* ``ANALYST_MODEL`` / ``ANALYST_FALLBACK_MODEL`` — ID моделей из списка организаторов.
* ``ANALYST_DAILY_BUDGET_RUB`` — потолок расхода команды за сутки (по умолчанию 400 из 500 ₽).
* ``ANALYST_CHECK_KEY_INFO`` — сверять расход с ``GET /key/info`` (по умолчанию да).
* ``ANALYST_SESSION_CAP_RUB`` — личный лимит процесса (для скриптов предрасчёта и замеров).
* ``ANALYST_MAX_TOKENS``, ``ANALYST_TIMEOUT_S``, ``ANALYST_MAX_RETRIES``, ``ANALYST_MAX_JSON_REPAIRS``.
* ``ANALYST_CACHE_DIR`` — рабочий кэш (запись); ``ANALYST_BUNDLED_CACHE_DIR`` — сохранённые
  ответы из репозитория (только чтение).
* ``ANALYST_PREFER_CACHE`` — в режиме ``llm`` сначала отдавать сохранённый ответ (экономия бюджета).
* ``ANALYST_AS_OF`` — фиксированный момент анализа (ISO), по умолчанию текущее время.
* ``ANALYST_FEED_HIDE_EXCLUDED`` — скрывать из ленты события с ``decision=exclude``.
* ``ANALYST_MAX_PARALLEL`` — сколько кандидатов ленты анализировать параллельно.
* ``ANALYST_PROMPT_VERSION`` — версия промпта: ``analyst-v1.0`` (по умолчанию, под неё
  сохранены ответы в ``app/analyst/cache``) или ``analyst-v1.1`` (вход ретривера, память фактов).
* ``ANALYST_MEMORY`` — память фактов и «что изменилось с прошлого» (по умолчанию выключена;
  работает с промптом ``analyst-v1.1``). ``ANALYST_MEMORY_PATH`` — файл SQLite
  (по умолчанию ``/data/analyst_memory.sqlite3`` или временная папка ОС).
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping

PACKAGE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = PACKAGE_DIR.parents[1]
DEFAULT_BASE_URL = "https://api.alfahack-gateway26.ru/v1"
DEFAULT_FALLBACK_BASE_URL = "https://changellenge.up.railway.app/v1"
# Выбрано замером на 6 кейсах (см. eval/results.json); запасная — следующая по качеству.
DEFAULT_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_FALLBACK_MODEL = "deepseek/deepseek-v4.1-flash"
VALID_MODES = ("template", "llm", "cached")
DEFAULT_PROMPT_VERSION = "analyst-v1.0"
VALID_PROMPT_VERSIONS = ("analyst-v1.0", "analyst-v1.1")

_dotenv_loaded = False


def _load_dotenv_once() -> None:
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv есть в requirements
        return
    env_file = BACKEND_DIR / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "да")


def _float(value: str | None, default: float | None) -> float | None:
    if value is None or value.strip() == "":
        return default
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return default


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _default_runtime_cache_dir() -> Path:
    """Рабочий кэш: том ``/data`` в Docker, иначе временная папка ОС."""
    data_dir = Path("/data")
    if data_dir.is_dir() and os.access(data_dir, os.W_OK):
        return data_dir / "analyst_cache"
    return Path(tempfile.gettempdir()) / "alfa_analyst_cache"


@dataclass(frozen=True)
class AnalystSettings:
    mode: str = "template"
    api_key: str = field(default="", repr=False)
    base_url: str = DEFAULT_BASE_URL
    fallback_base_url: str = DEFAULT_FALLBACK_BASE_URL
    model: str = DEFAULT_MODEL
    fallback_model: str = DEFAULT_FALLBACK_MODEL
    daily_budget_rub: float = 400.0
    check_key_info: bool = True
    session_cap_rub: float | None = None
    max_tokens: int = 1600
    chat_max_tokens: int = 500
    timeout_s: float = 60.0
    max_retries: int = 2
    max_json_repairs: int = 2
    runtime_cache_dir: Path = field(default_factory=_default_runtime_cache_dir)
    bundled_cache_dir: Path = PACKAGE_DIR / "cache"
    prefer_cache: bool = True
    as_of: str | None = None
    feed_hide_excluded: bool = True
    max_parallel: int = 4
    news_window_hours: int = 24 * 60
    prompt_version: str = DEFAULT_PROMPT_VERSION
    memory_enabled: bool = False
    memory_path: Path | None = None

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def with_overrides(self, **kwargs: object) -> "AnalystSettings":
        return replace(self, **kwargs)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "AnalystSettings":
        """Читает настройки. ``env=None`` — из ``os.environ`` (с подгрузкой ``backend/.env``)."""
        if env is None:
            _load_dotenv_once()
            env = os.environ
        mode = (env.get("ANALYST_MODE") or "template").strip().lower()
        if mode not in VALID_MODES:
            mode = "template"
        runtime_dir = env.get("ANALYST_CACHE_DIR")
        bundled_dir = env.get("ANALYST_BUNDLED_CACHE_DIR")
        prompt_version = (env.get("ANALYST_PROMPT_VERSION") or DEFAULT_PROMPT_VERSION).strip()
        if prompt_version not in VALID_PROMPT_VERSIONS:
            prompt_version = DEFAULT_PROMPT_VERSION
        memory_path = env.get("ANALYST_MEMORY_PATH")
        return cls(
            mode=mode,
            api_key=(env.get("LLM_API_KEY") or "").strip(),
            base_url=(env.get("LLM_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/"),
            fallback_base_url=(
                env.get("LLM_FALLBACK_BASE_URL", DEFAULT_FALLBACK_BASE_URL) or ""
            ).strip().rstrip("/"),
            model=(env.get("ANALYST_MODEL") or DEFAULT_MODEL).strip(),
            fallback_model=(env.get("ANALYST_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL).strip(),
            daily_budget_rub=_float(env.get("ANALYST_DAILY_BUDGET_RUB"), 400.0) or 400.0,
            check_key_info=_bool(env.get("ANALYST_CHECK_KEY_INFO"), True),
            session_cap_rub=_float(env.get("ANALYST_SESSION_CAP_RUB"), None),
            max_tokens=_int(env.get("ANALYST_MAX_TOKENS"), 1600),
            chat_max_tokens=_int(env.get("ANALYST_CHAT_MAX_TOKENS"), 500),
            timeout_s=_float(env.get("ANALYST_TIMEOUT_S"), 60.0) or 60.0,
            max_retries=_int(env.get("ANALYST_MAX_RETRIES"), 2),
            max_json_repairs=min(2, _int(env.get("ANALYST_MAX_JSON_REPAIRS"), 2)),
            runtime_cache_dir=Path(runtime_dir) if runtime_dir else _default_runtime_cache_dir(),
            bundled_cache_dir=Path(bundled_dir) if bundled_dir else PACKAGE_DIR / "cache",
            prefer_cache=_bool(env.get("ANALYST_PREFER_CACHE"), True),
            as_of=(env.get("ANALYST_AS_OF") or None),
            feed_hide_excluded=_bool(env.get("ANALYST_FEED_HIDE_EXCLUDED"), True),
            max_parallel=max(1, _int(env.get("ANALYST_MAX_PARALLEL"), 4)),
            news_window_hours=_int(env.get("ANALYST_NEWS_WINDOW_HOURS"), 24 * 60),
            prompt_version=prompt_version,
            memory_enabled=_bool(env.get("ANALYST_MEMORY"), False),
            memory_path=Path(memory_path) if memory_path else None,
        )
