#!/usr/bin/env python3
"""Минимальная проверка подключения к LLM-шлюзу (тратит несколько токенов).

Вызывает одну модель с коротким промптом и печатает: статус, модель, ответ,
ключ (маскированный), остаток дневного бюджета. Используется для проверки,
что API-ключ и base_url из .env корректны, ДО массового извлечения фактов.

Запуск из корня проекта:
    .venv_news/bin/python news/llm/probe.py
    .venv_news/bin/python news/llm/probe.py --model deepseek/deepseek-v4.1-flash
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from news.llm import budget, client, config, model_policy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Модель из списка разрешённых (по умолчанию из .env)")
    parser.add_argument("--prompt", default="Ответь одним коротким словом: работает?",
                        help="Короткий промпт для проверки (минимум токенов)")
    args = parser.parse_args()

    model = args.model or config.get_model()
    try:
        model_policy.resolve_route("extract_classify")  # валидируем конфиг модели
    except ValueError as e:
        print(f"Конфигурация модели: {e}")
        return 2

    print("Конфигурация LLM:")
    for k, v in config.summarize().items():
        print(f"  {k}: {v}")
    print(f"Бюджет: {budget.summary()}")
    print(f"Модель для проверки: {model}")

    if not budget.can_call():
        print("Дневной бюджет локально исчерпан — не вызываю.")
        return 3

    try:
        answer = client.chat(
            model,
            [{"role": "user", "content": args.prompt}],
            max_tokens=24,
            temperature=0.0,
        )
    except client.BudgetExceeded:
        print("Ошибка: дневной лимит бюджета исчерпан (на стороне шлюза).")
        return 3
    except client.LLMError as e:
        print(f"Ошибка подключения: {e}")
        return 4

    print("Ответ модели:", answer)
    print("Связь с LLM-шлюзом работает.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())