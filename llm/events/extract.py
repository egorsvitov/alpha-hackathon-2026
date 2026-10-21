"""B3. Извлечение утверждений (SourceClaim) из статьи через разрешённую LLM.

Один основной запрос на обычную статью. LLM возвращает массив утверждений с
цитатами; точные символьные позиции цитат определяет код (B4). Здесь — вызов
модели, парсинг JSON-ответа и сборка SourceClaim-кандидатов.

Бюджет: расход контролируется через news/llm/budget.py. При исчерпании
локального лимита статьи не обрабатываются (остаются ожидающими), а не
сжигаются зря.
"""
from __future__ import annotations

import json
from typing import Any

from news.core.model import Article
from news.llm import budget, client, config, model_policy

from llm.events.models import SourceClaim
from llm.events.state import BatchResult

# Промпт извлечения: требует строгую структуру и цитаты.
SYSTEM_PROMPT = """Ты — модуль извлечения экономических событий из русскоязычных новостей.
Извлеки из статьи самостоятельные утверждения (source claims). Каждое утверждение —
атомарный факт: действие/решение/показатель с участниками, временем, измерениями.
Разделяй разные действия и разные показатели. Сохраняй модальность (факт, прогноз,
план, мнение) и отрицание. НЕ выдумывай числа и даты; неизвестное оставляй null.
Цитируй исходный текст для каждого существенного поля (точные фрагменты).
Ответ — строго JSON-массив объектов с полями:
text, modality, polarity, stage, attribution, participants, action, object,
geographies, time, measurements, evidence (list of {field, quote}).
Используй только значения из статьи. Для каждого поля в evidence укажи дословную цитату.
"""


class ExtractionError(RuntimeError):
    """Ошибка извлечения (некорректный ответ, не JSON, пусто)."""


def _prompt_for(article: Article) -> str:
    return (
        f"Заголовок: {article.title}\n"
        f"Дата публикации: {article.published_at or 'неизвестно'}\n"
        f"Источник: {article.source}\n\n"
        f"Текст статьи:\n{article.text}\n\n"
        "Извлеки утверждения в формате JSON-массива."
    )


def _normalize_answer(raw: str) -> list[dict]:
    """Разбирает ответ модели в список словарей утверждений."""
    text = raw.strip()
    # Убираем возможные markdown-ограждения ```json ... ```.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ExtractionError("Ответ модели не содержит JSON-массив")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ExtractionError(f"Некорректный JSON в ответе модели: {e}") from e
    if not isinstance(data, list):
        raise ExtractionError("Ответ модели — не массив")
    return [d for d in data if isinstance(d, dict)]


def _build_claim(idx: int, raw: dict, article_id: str) -> SourceClaim | None:
    """Собирает SourceClaim из словаря модели, пропуская пустые."""
    text = str(raw.get("text") or "").strip()
    if not text:
        return None
    participants = []
    for p in raw.get("participants", []) or []:
        if isinstance(p, dict) and (p.get("name")):
            participants.append({
                "entity_id": p.get("entity_id"),
                "name": p["name"],
                "role": p.get("role") or "unknown",
            })
    measurements = []
    for m in raw.get("measurements", []) or []:
        if isinstance(m, dict) and m.get("metric") is not None:
            measurements.append({
                "measurement_id": f"{article_id}:claim-{idx}:m{len(measurements)+1}",
                "metric": m["metric"],
                "kind": m.get("kind") or "level",
                "value": m.get("value"),
                "unit": m.get("unit") or "",
                "qualifier": m.get("qualifier") or "exact",
                "comparison_period": m.get("comparison_period") or None,
            })
    return SourceClaim(
        claim_id=f"{article_id}:claim-{idx+1}",
        text=text,
        modality=str(raw.get("modality") or "asserted"),
        polarity=str(raw.get("polarity") or "affirmed"),
        stage=raw.get("stage"),
        attribution=raw.get("attribution"),
        participants=participants,
        action=raw.get("action"),
        object=raw.get("object"),
        geographies=list(raw.get("geographies") or []),
        time=raw.get("time"),
        measurements=measurements,
        evidence_ids=[f"{article_id}:ev{i+1}" for i in range(len(raw.get("evidence", []) or []))],
        field_evidence={},
    )


def extract_article(article: Article) -> list[SourceClaim]:
    """Извлекает SourceClaim из одной статьи через LLM (B3).

    Кидает ExtractionError при проблемах; BudgetExceeded при лимите бюджета.
    """
    if not budget.can_call():
        raise client.BudgetExceeded("Локальный дневной лимит бюджета исчерпан.")

    model = model_policy.resolve_route("extract_classify")
    try:
        raw = client.chat(
            model,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _prompt_for(article)},
            ],
            max_tokens=1500,
            temperature=0.0,
        )
    except client.BudgetExceeded:
        raise
    except client.LLMError as e:
        raise ExtractionError(f"Ошибка LLM: {e}") from e

    # Грубая оценка расхода по длине ответа (не точная цена).
    budget.record_usage(output_tokens=max(1, len(raw) // 4))

    claims = []
    for idx, raw_claim in enumerate(_normalize_answer(raw)):
        c = _build_claim(idx, raw_claim, article.article_id)
        if c:
            claims.append(c)
    return claims