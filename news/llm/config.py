"""Конфигурация доступа к LLM (track «Новостная аналитика»).

Ключ НЕ хранится в коде и НЕ логируется в открытом виде. Он читается из
переменной окружения или файла .env через os/dotenv. Модуль предоставляет
функцию mask_key(), которая маскирует ключ в любых логах и исключениях.

Настройки подключения (из инструкции трека):
  - LLM_API_KEY    — ключ, начинается с sk-;
  - LLM_BASE_URL   — https://changellenge.up.railway.app/v1
                     (запасной https://api.alfahack-gateway26.ru/v1);
  - LLM_MODEL      — одна из разрешённых моделей (см. news/llm/model_policy.py);
  - LLM_DAILY_BUDGET_RUB — дневной бюджет 500 ₽, сброс в 03:00 МСК;
  - LLM_ENABLED    — разрешать ли реальные вызовы (false = dry-run).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Загружаем .env из папки news/ (там хранятся LLM-настройки пайплайна).
# Не читаем корневой .env backend: у него extra="forbid", и чужие переменные
# ломают запуск backend (например test_e2e).
_NEWS_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_NEWS_ROOT / ".env")

# Константы по умолчанию (значения из инструкции трека).
DEFAULT_BASE_URL = "https://changellenge.up.railway.app/v1"
DEFAULT_MODEL = "qwen/qwen3.6-35b-a3b"
DAILY_BUDGET_RUB = 500.0


def get_api_key() -> str | None:
    """Возвращает ключ LLM из окружения/.env либо None."""
    return os.getenv("LLM_API_KEY") or None


def get_base_url() -> str:
    return os.getenv("LLM_BASE_URL") or DEFAULT_BASE_URL


def get_model() -> str:
    return os.getenv("LLM_MODEL") or DEFAULT_MODEL


def get_daily_budget_rub() -> float:
    try:
        return float(os.getenv("LLM_DAILY_BUDGET_RUB") or DAILY_BUDGET_RUB)
    except (TypeError, ValueError):
        return DAILY_BUDGET_RUB


def llm_enabled() -> bool:
    raw = os.getenv("LLM_ENABLED")
    return raw is None or raw.lower() in ("1", "true", "yes", "on")


def mask_key(key: str | None) -> str:
    """Маскирует ключ для логов: sk-QMhj******g9g. Пустой/None → '(empty)'."""
    if not key:
        return "(empty)"
    if len(key) <= 6:
        return "*" * len(key)
    return f"{key[:6]}******{key[-3:]}"


def summarize() -> dict:
    """Краткая сводка конфигурации для лога запуска (ключ маскирован)."""
    return {
        "base_url": get_base_url(),
        "model": get_model(),
        "daily_budget_rub": get_daily_budget_rub(),
        "enabled": llm_enabled(),
        "api_key": mask_key(get_api_key()),
    }