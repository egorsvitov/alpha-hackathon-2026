"""Учёт дневного бюджета LLM (общий на команду, 500 ₽/день).

Проверяет лимит локально, чтобы не слать запросы, когда дневная квота уже
исчерпана (сброс в 03:00 МСК). Оценка расхода — по токенам ответа, пока у
шлюза нет цены за токен (pricing_status = estimated).

Состояние хранится в локальном JSON-файле в data/processed/llm_budget.json,
поэтому переживает перезапуск процесса. Это НЕ замена серверного контроля —
шлюз всё равно отклонит запрос при превышении (ошибка Budget has been exceeded).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from news.llm import config

# Время сброса бюджета в МСК (03:00). Храним локально для консервативной проверки.
RESET_HOUR_MSK = 3


def _budget_path() -> Path:
    return Path(os.getenv("LLM_BUDGET_FILE", "data/processed/llm_budget.json"))


def _load() -> dict:
    p = _budget_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save(state: dict) -> None:
    p = _budget_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_msk_date() -> str:
    """Текущая дата в МСК (без внешних зависимостей, грубо по UTC+3)."""
    from datetime import datetime, timezone, timedelta
    msk = timezone(timedelta(hours=3))
    return datetime.now(msk).strftime("%Y-%m-%d")


def spent_today() -> float:
    state = _load()
    if state.get("date") != _now_msk_date():
        return 0.0
    return float(state.get("spent_rub", 0.0))


def remaining() -> float:
    return max(0.0, config.get_daily_budget_rub() - spent_today())


def can_call() -> bool:
    return remaining() > 0


def record_usage(input_tokens: int = 0, output_tokens: int = 0, est_cost_rub: float = 0.0) -> None:
    """Добавляет оценку расхода за один вызов в локальный учёт."""
    state = _load()
    today = _now_msk_date()
    if state.get("date") != today:
        state = {"date": today, "spent_rub": 0.0, "input_tokens": 0, "output_tokens": 0}
    state["spent_rub"] = float(state.get("spent_rub", 0.0)) + est_cost_rub
    state["input_tokens"] = int(state.get("input_tokens", 0)) + input_tokens
    state["output_tokens"] = int(state.get("output_tokens", 0)) + output_tokens
    _save(state)


def estimate_cost(output_tokens: int, rate_rub_per_1k: float = 0.0) -> float:
    """Грубая оценка стоимости по токенам ответа.

    rate_rub_per_1k — цена за 1000 токенов. Если неизвестна (0), возвращаем 0,
    а реальный контроль оставляем серверному шлюзу.
    """
    return (output_tokens / 1000.0) * rate_rub_per_1k


def summary() -> dict:
    return {
        "daily_budget_rub": config.get_daily_budget_rub(),
        "spent_today_rub": round(spent_today(), 4),
        "remaining_rub": round(remaining(), 4),
        "reset_local_time_msk": f"03:00",
        "estimated_pricing": True,
    }