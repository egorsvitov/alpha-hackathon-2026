"""LLM-подсистема пайплайна новостей: доступ, политика моделей, бюджет.

Задачи:
  - config — чтение ключа/base_url/модели через os/.env (ключ не в коде);
  - model_policy — выбор разрешённых моделей по задаче;
  - client — тонкий OpenAI-compatible клиент с маскировкой ключа;
  - budget — локальный учёт дневного бюджета 500 ₽ (сброс 03:00 МСК).
"""
from news.llm import budget, client, config, model_policy

__all__ = ["budget", "client", "config", "model_policy"]