"""Формирование тематического плана регулярного батча новостей по аудитории.

Принимает AudienceProfile (результат анализа CSV) и текущий реестр источников,
определяет, какие темы/отрасли/регионы/страны ВЭД приоритетны, и возвращает
план батча: какие категории источников включить, какие темы искать, какие
регионы/страны учитывать.

Это «тематическое покрытие» поверх существующего реестра: не добавляет новых
источников, а настраивает, что собирать и как ранжировать для данной аудитории.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from news.profile.audience import AudienceProfile

# Приоритетные категории источников для разных профилей (макрос для всех).
BASE_CATEGORIES = ["macro", "business"]

# Маппинг отрасли (по ключевым словам MAINOKVED_NAME) → тема/категория.
# Служит для понимания, какие отраслевые источники/темы важны.
INDUSTRY_TO_TOPIC = {
    "недвижим": "realestate",
    "строител": "construction",
    "торговл": "trade",
    "ресторан": "hospitality",
    "гостиниц": "hospitality",
    "программн": "it",
    "компьютерн": "it",
    "перевоз": "logistics",
    "грузов": "logistics",
    "научн": "rnd",
    "рекламн": "marketing",
    "фармацевт": "pharma",
    "финанс": "finance",
    "нефтегаз": "oil_gas",
    "металлург": "metallurgy",
    "сельск": "agro",
}

# Приоритетные страны ВЭД для новостей о внешней торговле и логистике.
TRADE_PRIORITY_COUNTRIES = [
    "КИТАЙ", "ТУРЦИЯ", "ИНДИЯ", "ОАЭ", "УЗБЕКИСТАН", "ГОНКОНГ",
    "ЮЖНАЯ КОРЕЯ", "КАЗАХСТАН", "БЕЛАРУСЬ", "ГЕРМАНИЯ",
]


@dataclass
class BatchPlan:
    """План регулярного батча новостей, настроенный под аудиторию."""

    categories: list[str] = field(default_factory=list)
    top_topics: list[str] = field(default_factory=list)          # темы из отраслей
    priority_countries: list[str] = field(default_factory=list)  # страны ВЭД
    top_regions: list[str] = field(default_factory=list)
    source_names: list[str] = field(default_factory=list)        # отобрать эти источники
    reasoning: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "categories": self.categories,
            "top_topics": self.top_topics,
            "priority_countries": self.priority_countries,
            "top_regions": self.top_regions,
            "source_names": self.source_names,
            "reasoning": self.reasoning,
        }


def _topic_of_industry(name: str) -> str | None:
    low = name.lower()
    for key, topic in INDUSTRY_TO_TOPIC.items():
        if key in low:
            return topic
    return None


def build_batch_plan(profile: AudienceProfile, sources=None, *,
                     top_n_industries: int = 8) -> BatchPlan:
    """Строит план батча из профиля аудитории и списка источников.

    sources — список SourceSpec из реестра; если передан, дополнительно
    отбирает имена источников, покрывающие приоритетные категории.
    """
    plan = BatchPlan()
    plan.categories = list(BASE_CATEGORIES)
    plan.reasoning.append(
        f"Базовая категория: {', '.join(BASE_CATEGORIES)} (макро + деловые) — значимы для всех компаний."
    )

    # Темы из преобладающих отраслей.
    topic_counts: dict[str, int] = {}
    for name, cnt in profile.top_industries[:top_n_industries]:
        t = _topic_of_industry(name)
        if t:
            topic_counts[t] = topic_counts.get(t, 0) + cnt
    plan.top_topics = [t for t, _ in sorted(topic_counts.items(), key=lambda x: -x[1])]
    if plan.top_topics:
        plan.reasoning.append(
            f"Темы из отраслей аудитории: {', '.join(plan.top_topics)}."
        )

    # Страны ВЭД: приоритетные + из профиля.
    seen: set[str] = set()
    for name, cnt in profile.top_trade_countries:
        if name in TRADE_PRIORITY_COUNTRIES and name not in seen:
            plan.priority_countries.append(name)
            seen.add(name)
    for name in TRADE_PRIORITY_COUNTRIES:
        if name in seen:
            continue
        # добавляем приоритетные страны даже если не в топе (важность для ВЭД)
        plan.priority_countries.append(name)
        seen.add(name)
    if profile.fts_share > 0.05:
        plan.reasoning.append(
            f"Доля компаний с ВЭД {profile.fts_share:.0%} — учитываем внешнюю торговлю: "
            f"{', '.join(plan.priority_countries[:6])}."
        )

    # Регионы.
    plan.top_regions = [n for n, _ in profile.top_regions[:5]]
    if plan.top_regions:
        plan.reasoning.append(f"Регионы концентрации: {', '.join(plan.top_regions)}.")

    # Отбор источников из реестра по категориям.
    if sources:
        for s in sources:
            if s.category in plan.categories:
                plan.source_names.append(s.name)
        plan.reasoning.append(
            f"Источники по категориям: {', '.join(plan.source_names)}."
        )

    return plan