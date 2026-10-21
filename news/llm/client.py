"""Тонкий клиент к OpenAI-compatible LLM gateway.

Оборачивает openai.OpenAI, читая ключ/base_url/model из конфигурации (os/.env),
маскирует ключ в логах и учитывает дневной бюджет. Не реализует сам трекинг
расхода — для этого отдельный модуль budget.py; здесь только проверка
«разрешено ли вызвать» и обработка ошибки лимита.

Важно: ключ никогда не попадает в код или логи в открытом виде.
"""
from __future__ import annotations

from openai import OpenAI

from news.llm import config


class LLMError(RuntimeError):
    """Ошибка обращения к LLM (не включать в сообщение открытый ключ)."""


class BudgetExceeded(LLMError):
    """Дневной лимит бюджета исчерпан (на стороне шлюза или локально)."""


def build_client() -> OpenAI:
    key = config.get_api_key()
    if not key:
        raise LLMError("Не задан LLM_API_KEY (см. .env). Ключ должен начинаться с sk-.")
    return OpenAI(api_key=key, base_url=config.get_base_url())


def chat(model: str, messages: list[dict], *, max_tokens: int = 700, temperature: float = 0.2) -> str:
    """Один chat-запрос. Возвращает текст ответа.

    messages — список вида [{"role": "user", "content": "..."}].
    Кидает BudgetExceeded при ошибке лимита со стороны шлюза.
    """
    if not config.llm_enabled():
        raise LLMError("LLM отключён (LLM_ENABLED=false) — это dry-run.")
    client = build_client()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "Budget" in msg or "budget" in msg or "limit" in msg.lower():
            raise BudgetExceeded("Дневной лимит бюджета исчерпан.") from e
        raise LLMError(f"Ошибка LLM-запроса: {msg}") from e
    content = resp.choices[0].message.content
    return (content or "").strip()