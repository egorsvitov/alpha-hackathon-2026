"""Политика выбора LLM для задач извлечения/классификации.

Список разрешённых моделей совпадает с приложением 3 кейса и инструкцией
трека. Здесь модели назначаются задачам по сложности (как в
llm/agent/model_policy.json, но для пайплайна новостей).

Расход бюджета общий на команду и на все модели. Для массового извлечения
фактов из потока новостей выбирается дешёвая/универсальная модель; для
сложной интерпретации — более мощная. Значения можно переопределить через
LLM_MODEL или точечно через переменные LLM_MODEL_EXTRACT и LLM_MODEL_EDIT.
"""
from __future__ import annotations

import os

# Полный перечень разрешённых моделей (точные ID из инструкции).
ALLOWED_MODELS = {
    "google/gemma-4-26b-a4b-it",
    "z-ai/glm-5.3-flash",
    "qwen/qwen3.6-35b-a3b",
    "deepseek/deepseek-v4.1-flash",
    "qwen/qwen3-vl-30b-a3b-instruct",
    "moonshotai/kimi-k2.6",
}

# Назначение по умолчанию: задача -> модель.
ROUTES = {
    # Классификация и извлечение фактов: универсальная недорогая модель.
    "extract_classify": "qwen/qwen3.6-35b-a3b",
    # Редактура/уточнение текста: flash-модель.
    "edit": "z-ai/glm-5.3-flash",
    # Анализ изображений/графиков в статьях: VL-модель.
    "vision": "qwen/qwen3-vl-30b-a3b-instruct",
}


def resolve_route(task: str) -> str:
    """Возвращает модель для задачи. Точечные переменные env имеют приоритет.

    Задача вне маршрутов или переменная задаёт неразрешённую модель —
    падаем явно, чтобы случайно не сжечь бюджет чужой моделью.
    """
    if task not in ROUTES:
        raise ValueError(f"Неизвестная задача LLM: {task}")
    override = os.getenv(f"LLM_MODEL_{task.upper()}")
    model = override or os.getenv("LLM_MODEL") or ROUTES[task]
    if model not in ALLOWED_MODELS:
        raise ValueError(f"Модель не в списке разрешённых: {model}")
    return model


def allowed(model: str) -> bool:
    return model in ALLOWED_MODELS