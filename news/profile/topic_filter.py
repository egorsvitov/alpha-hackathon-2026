"""Тематический фильтр кандидатов по плану батча (из профиля аудитории).

Позволяет собирать не всё подряд, а только релевантные целевой аудитории
новости: отбирает кандидатов, чей заголовок/описание затрагивает темы,
отрасли, страны ВЭД или регионы из BatchPlan.

Фильтрация лёгкая (по ключевым словам, без LLM), поэтому не тратит бюджет и
может выполняться на этапе сбора. Не отбрасывает, а только сокращает поток.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from news.profile.batch_plan import BatchPlan

# Ключевые слова для тем (расширяется по мере добавления отраслей).
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "realestate": ["недвижим", "жил", "офис", "строй", "аренд", "ипотек"],
    "construction": ["строител", "застройщ", "подряд", "жкх", "смет", "долгострой"],
    "trade": ["торговл", "ритейл", "ретейл", "импорт", "экспорт", "поставк"],
    "hospitality": ["ресторан", "гостиниц", "общепит", "туризм", "отель", "кафе"],
    "it": ["программн", "софт", "разработ", "цифров", "ии", "нейросет", "it", "стартап"],
    "logistics": ["перевоз", "логистик", "груз", "транспорт", "порт", "судоход"],
    "finance": ["банк", "кредит", "ставк", "финанс", "вклад", "ликвидност"],
    "rates": ["ключевую ставк", "ставк", "цб", "центробанк", "денежно-кредит"],
    "inflation": ["инфляц", "цены выросли", "потребительск", "темп роста цен"],
    "currency": ["курс рубл", "валюта", "доллар", "евро", "рубль"],
    "sanctions": ["санкци", "ограничени", "эмбарго", "санкцион"],
    "regulation": ["регулирован", "закон", "постановлен", "минфин", "правительств"],
    "oil_gas": ["нефт", "газ", "трубопровод", "топлив"],
    "metallurgy": ["металл", "стал", "прокат", "алюмин"],
    "agro": ["сельск", "зерно", "урожай", "аграр"],
    "pharma": ["фармацевт", "лекарств", "медпрепар"],
    "marketing": ["реклам", "маркетинг", "продвиж"],
    "rnd": ["исследовани", "научн", "разработк"],
}

# Имена стран (нормализованные, для регистрационных новостей о ВЭД).
COUNTRY_ALIASES: dict[str, list[str]] = {
    "КИТАЙ": ["кита", "хэнань", "шанхай", "пэкин", "китайск"],
    "ТУРЦИЯ": ["турц"],
    "ИНДИЯ": ["инди"],
    "ОАЭ": ["оаэ", "дубай", "эмират"],
    "УЗБЕКИСТАН": ["узбек"],
    "ГОНКОНГ": ["гонконг"],
    "ЮЖНАЯ КОРЕЯ": ["коре"],
    "КАЗАХСТАН": ["казах"],
    "БЕЛАРУСЬ": ["белорус"],
    "ГЕРМАНИЯ": ["герман", "немец"],
}


@dataclass
class TopicFilter:
    """Фильтр кандидатов по темам/странам/регионам плана батча."""

    plan: BatchPlan
    include_topics: bool = True
    include_countries: bool = True
    # Безусловно релевантные макро-темы: важны всем компаниям независимо от отрасли.
    # Если новость затрагивает любую из них — пропускаем её всегда.
    always_topics: tuple[str, ...] = (
        "finance", "rates", "inflation", "currency", "sanctions", "regulation",
    )

    def _compile_patterns(self) -> tuple[list[str], list[str]]:
        topic_pats: list[str] = []
        for t in self.plan.top_topics:
            for kw in TOPIC_KEYWORDS.get(t, []):
                topic_pats.append(re.escape(kw))
        always_pats: list[str] = []
        for t in self.always_topics:
            for kw in TOPIC_KEYWORDS.get(t, []):
                always_pats.append(re.escape(kw))
        country_pats: list[str] = []
        for country in self.plan.priority_countries:
            for alias in COUNTRY_ALIASES.get(country, []):
                country_pats.append(re.escape(alias))
        return topic_pats, country_pats, always_pats

    def matches(self, title: str, summary: str = "") -> bool:
        """True, если кандидат релевантен плану (тема, страна ИЛИ макро-тема)."""
        text = f"{title} {summary}".lower()
        topic_pats, country_pats, always_pats = self._compile_patterns()
        # Макро-темы (ставка, инфляция, санкции, регуляторика) — всегда релевантны.
        if always_pats and any(p in text for p in always_pats):
            return True
        if self.include_topics and topic_pats:
            if any(p in text for p in topic_pats):
                return True
        if self.include_countries and country_pats:
            if any(p in text for p in country_pats):
                return True
        # Ничего не задано — пропускаем всё (не фильтруем).
        return not topic_pats and not country_pats

    def filter_candidates(self, candidates) -> list:
        """Отбирает кандидатов, релевантных плану."""
        out = []
        for c in candidates:
            summary = getattr(c, "summary", "") or ""
            if self.matches(c.title, summary):
                out.append(c)
        return out