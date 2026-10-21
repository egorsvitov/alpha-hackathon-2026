"""Профилирование целевой аудитории и формирование плана новостных батчей.

  - audience.py    — анализ CSV (юрлица/физлица/платежи) → AudienceProfile;
  - batch_plan.py  — профиль → план регулярного батча (темы/регионы/страны/источники);
  - topic_filter.py— фильтр кандидатов по плану (без LLM, на этапе сбора).

Данные читаются из news/*.csv локально (не в git).
"""
from news.profile import audience, batch_plan, topic_filter

__all__ = ["audience", "batch_plan", "topic_filter"]