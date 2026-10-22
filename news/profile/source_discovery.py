"""Поиск и подбор новых источников новостей под целевую аудиторию.

Идея: аудитория (из CSV) задаёт приоритетные темы — отрасли, страны ВЭД,
регионы. Каждая тема соответствует определённому типу источников. Этот модуль
по профилю аудитории формирует рекомендации: какие источники уже покрывают
нужные темы, каких тематических ниш не хватает и какие потенциальные источники
(из каталога) стоит проверить/добавить в реестр.

Не выполняет сбор и не тратит LLM — только анализирует профиль и каталог.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from news.profile.audience import AudienceProfile
from news.profile.batch_plan import INDUSTRY_TO_TOPIC

# Тема → потенциальные источники (реальные RSS-ленты, проверяемые на доступность).
# Каждый источник: имя, url, kind, category, к каким темам относится.
POTENTIAL_SOURCES: dict[str, list[dict]] = {
    "realestate": [
        {"name": "rbc_realty", "url": "https://realty.rbc.ru/rss/", "kind": "rss", "category": "industry"},
        {"name": "realty_ria", "url": "https://realty.ria.ru/export/articles/rss/", "kind": "rss", "category": "industry"},
    ],
    "construction": [
        {"name": "metaltorg", "url": "https://www.metaltorg.ru/news/rss/", "kind": "rss", "category": "industry"},
        {"name": "rbc_realty", "url": "https://realty.rbc.ru/rss/", "kind": "rss", "category": "industry"},
    ],
    "trade": [
        {"name": "retailer_ru", "url": "https://www.retail.ru/rss/", "kind": "rss", "category": "industry"},
        {"name": "sostav", "url": "https://www.sostav.ru/rss/", "kind": "rss", "category": "industry"},
    ],
    "hospitality": [
        {"name": "tourdom", "url": "https://www.tourdom.ru/rss/", "kind": "rss", "category": "industry"},
        {"name": "prof_tour", "url": "https://profi.travel/rss", "kind": "rss", "category": "industry"},
    ],
    "logistics": [
        {"name": "logirus", "url": "https://www.logirus.ru/rss", "kind": "rss", "category": "industry"},
        {"name": "transinfo", "url": "https://transinfo.info/rss/", "kind": "rss", "category": "industry"},
    ],
    "it": [
        {"name": "cnews", "url": "https://www.cnews.ru/inc/rss/news.xml", "kind": "rss", "category": "industry"},
        {"name": "3dnews", "url": "https://3dnews.ru/news/rss/", "kind": "rss", "category": "industry"},
    ],
    "finance": [
        {"name": "bankir", "url": "https://www.bankir.ru/rss/", "kind": "rss", "category": "macro"},
        {"name": "finmarket", "url": "https://www.finmarket.ru/rss/", "kind": "rss", "category": "macro"},
    ],
    "oil_gas": [
        {"name": "oilcapital", "url": "https://oilcapital.ru/rss", "kind": "rss", "category": "industry"},
        {"name": "nangs", "url": "https://www.nangs.org/rss", "kind": "rss", "category": "industry"},
    ],
    "metallurgy": [
        {"name": "metaltorg", "url": "https://www.metaltorg.ru/rss/", "kind": "rss", "category": "industry"},
        {"name": "metalinfo", "url": "https://www.metalinfo.ru/ru/rss", "kind": "rss", "category": "industry"},
    ],
    "agro": [
        {"name": "agroxxi", "url": "https://www.agroxxi.ru/rss/", "kind": "rss", "category": "industry"},
        {"name": "agrovesti", "url": "https://agrovesti.net/rss", "kind": "rss", "category": "industry"},
    ],
    "pharma": [
        {"name": "pharmvestnik", "url": "https://pharmvestnik.ru/rss", "kind": "rss", "category": "industry"},
        {"name": "vademec", "url": "https://vademec.ru/rss/", "kind": "rss", "category": "industry"},
    ],
    "marketing": [
        {"name": "sostav", "url": "https://www.sostav.ru/rss/", "kind": "rss", "category": "industry"},
    ],
    "rnd": [
        {"name": "nauka", "url": "https://indicator.ru/rss/", "kind": "rss", "category": "industry"},
    ],
    "rates": [
        {"name": "cbr_rss", "url": "https://www.cbr.ru/rss/RssNews", "kind": "rss", "category": "official"},
        {"name": "cbr_press", "url": "https://www.cbr.ru/rss/RssPress", "kind": "rss", "category": "official"},
    ],
    "inflation": [
        {"name": "1prime", "url": "https://1prime.ru/export/rss2/index.xml", "kind": "rss", "category": "macro"},
        {"name": "rg_econ", "url": "https://rg.ru/xml/rss/economics.xml", "kind": "rss", "category": "macro"},
    ],
    "sanctions": [
        {"name": "interfax", "url": "https://www.interfax.ru/rss.asp", "kind": "rss", "category": "business"},
    ],
}

# Обратный индекс: имя потенциального источника → темы, которые он покрывает.
def _source_to_topics() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for topic, sources in POTENTIAL_SOURCES.items():
        for s in sources:
            out.setdefault(s["name"], []).append(topic)
    return out


@dataclass
class SourceSuggestion:
    """Одна рекомендация нового источника."""

    name: str
    url: str
    kind: str
    category: str
    topics: list[str]
    coverage: int      # сколько компаний/покрытие темы у аудитории
    status: str = "candidate"   # candidate | added | skipped


@dataclass
class SourceDiscoveryResult:
    """Результат поиска новых источников под аудиторию."""

    current_topics: list[str] = field(default_factory=list)      # темы аудитории
    gaps: list[str] = field(default_factory=list)                # темы без покрытия
    suggestions: list[SourceSuggestion] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "current_topics": self.current_topics,
            "gaps": self.gaps,
            "suggestions": [
                {
                    "name": s.name, "url": s.url, "kind": s.kind, "category": s.category,
                    "topics": s.topics, "coverage": s.coverage, "status": s.status,
                }
                for s in self.suggestions
            ],
            "reasoning": self.reasoning,
        }


def _topics_from_profile(profile: AudienceProfile, top_n: int = 8) -> dict[str, int]:
    """Темы аудитории из преобладающих отраслей (MAINOKVED_NAME)."""
    topic_counts: dict[str, int] = {}
    for name, cnt in profile.top_industries[:top_n]:
        t = _topic_of_industry(name)
        if t:
            topic_counts[t] = topic_counts.get(t, 0) + cnt
    return topic_counts


def _topic_of_industry(name: str) -> str | None:
    low = name.lower()
    for key, topic in INDUSTRY_TO_TOPIC.items():
        if key in low:
            return topic
    return None


def discover_sources(profile: AudienceProfile,
                     existing_sources=None,
                     top_n_industries: int = 8) -> SourceDiscoveryResult:
    """Формирует рекомендации новых источников под аудиторию.

    existing_sources — список SourceSpec из реестра (имена уже добавленных).
    """
    res = SourceDiscoveryResult()

    # Темы аудитории.
    topic_counts = _topics_from_profile(profile, top_n_industries)
    res.current_topics = [t for t, _ in sorted(topic_counts.items(), key=lambda x: -x[1])]
    res.reasoning.append(
        f"Темы аудитории: {', '.join(res.current_topics) or '(не определены)'}."
    )

    existing_names = {s.name for s in existing_sources} if existing_sources else set()
    src_to_topics = _source_to_topics()

    # Для каждой темы аудитории смотрим потенциальные источники.
    coverage_by_topic: dict[str, int] = {}
    suggested: dict[str, SourceSuggestion] = {}

    for topic in res.current_topics:
        coverage_by_topic[topic] = topic_counts.get(topic, 0)
        for cand in POTENTIAL_SOURCES.get(topic, []):
            name = cand["name"]
            if name in existing_names:
                continue  # уже есть в реестре
            if name in suggested:
                suggested[name].topics.append(topic)
                suggested[name].coverage = max(suggested[name].coverage, coverage_by_topic[topic])
                continue
            suggested[name] = SourceSuggestion(
                name=name, url=cand["url"], kind=cand["kind"], category=cand["category"],
                topics=[topic], coverage=coverage_by_topic[topic],
            )

    res.suggestions = sorted(suggested.values(), key=lambda s: -s.coverage)

    # Ниши без потенциальных источников (gaps).
    covered = set()
    for t in res.current_topics:
        if POTENTIAL_SOURCES.get(t):
            covered.add(t)
    res.gaps = [t for t in res.current_topics if t not in covered]
    if res.gaps:
        res.reasoning.append(f"Ниши без источников: {', '.join(res.gaps)}.")

    if res.suggestions:
        res.reasoning.append(
            f"Предложено источников: {len(res.suggestions)} (топ: "
            f"{', '.join(s.name for s in res.suggestions[:8])})."
        )
    else:
        res.reasoning.append("Все темы аудитории уже покрыты текущими источниками.")

    return res


def suggest_source_json(profile: AudienceProfile, existing_sources=None,
                        top_n_industries: int = 8) -> dict:
    """Удобный JSON-вывод рекомендаций (для CLI и интеграции)."""
    return discover_sources(profile, existing_sources, top_n_industries).to_dict()