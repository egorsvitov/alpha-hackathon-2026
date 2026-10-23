"""Поиск и подбор новых источников новостей под целевую аудиторию.

Идея: аудитория (из CSV) задаёт приоритетные темы — отрасли, страны ВЭД,
регионы. Каждая тема и регион соответствуют определённому типу источников.
Этот модуль по профилю аудитории формирует рекомендации: какие источники уже
покрывают нужные темы и регионы ЦА, каких тематических/региональных ниш не
хватает и какие потенциальные источники (из каталога) стоит добавить в реестр.

Не выполняет сбор и не тратит LLM — только анализирует профиль и каталог.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from news.profile.audience import AudienceProfile
from news.profile.batch_plan import INDUSTRY_TO_TOPIC

# Тема → потенциальные источники (проверенные на доступность RSS-ленты).
# Каждый источник: имя, url, kind, category.
POTENTIAL_SOURCES: dict[str, list[dict]] = {
    "realestate": [
        {"name": "spb_nsp", "url": "https://nsp.ru/rss", "kind": "rss", "category": "industry"},
        {"name": "realty_ria", "url": "https://realty.ria.ru/export/articles/rss/", "kind": "rss", "category": "industry"},
    ],
    "construction": [
        {"name": "metaltorg", "url": "https://www.metaltorg.ru/news/rss/", "kind": "rss", "category": "industry"},
        {"name": "spb_nsp", "url": "https://nsp.ru/rss", "kind": "rss", "category": "industry"},
    ],
    "trade": [
        {"name": "retail_news", "url": "https://www.retailer.ru/rss/", "kind": "rss", "category": "industry"},
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
        {"name": "1prime", "url": "https://1prime.ru/export/rss2/index.xml", "kind": "rss", "category": "macro"},
        {"name": "gazprombank", "url": "https://www.gazprombank.ru/rss/", "kind": "rss", "category": "macro"},
    ],
    "oil_gas": [
        {"name": "oilcapital", "url": "https://oilcapital.ru/rss", "kind": "rss", "category": "industry"},
        {"name": "minenergo", "url": "https://minenergo.gov.ru/rss", "kind": "rss", "category": "official"},
    ],
    "metallurgy": [
        {"name": "metaltorg", "url": "https://www.metaltorg.ru/news/rss/", "kind": "rss", "category": "industry"},
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
        {"name": "cbr_event", "url": "https://www.cbr.ru/rss/eventrss", "kind": "rss", "category": "official"},
        {"name": "cbr_press", "url": "https://www.cbr.ru/rss/RssPress", "kind": "rss", "category": "official"},
    ],
    "inflation": [
        {"name": "1prime", "url": "https://1prime.ru/export/rss2/index.xml", "kind": "rss", "category": "macro"},
        {"name": "rg_econ", "url": "https://rg.ru/xml/rss/economics.xml", "kind": "rss", "category": "macro"},
    ],
    "sanctions": [
        {"name": "interfax_rss", "url": "https://www.interfax.ru/rss.asp", "kind": "rss", "category": "business"},
    ],
}

# Регион ЦА (по полю REGION из df_Uid_data_hack.csv) → проверенные живые RSS-ленты региональных СМИ.
REGIONAL_SOURCES: dict[str, list[dict]] = {
    "МОСКВА": [
        {"name": "m24_ru", "url": "https://www.m24.ru/rss.xml", "kind": "rss", "category": "regional"},
    ],
    "САНКТ-ПЕТЕРБУРГ": [
        {"name": "spb_peterburg2", "url": "https://peterburg2.ru/rss/", "kind": "rss", "category": "regional"},
        {"name": "spb_nsp", "url": "https://nsp.ru/rss", "kind": "rss", "category": "industry"},
    ],
    "ЛЕНИНГРАДСКАЯ": [
        {"name": "spb_online47", "url": "https://online47.ru/rss", "kind": "rss", "category": "regional"},
    ],
    "НОВОСИБИРСКАЯ": [
        {"name": "sib_infopro54", "url": "https://infopro54.ru/feed/", "kind": "rss", "category": "regional"},
    ],
    "КРАСНОЯРСКИЙ": [
        {"name": "sib_krsk", "url": "https://krsk.sibnovosti.ru/rss", "kind": "rss", "category": "regional"},
    ],
    "СВЕРДЛОВСКАЯ": [
        {"name": "ural_ean", "url": "https://eanews.ru/rss", "kind": "rss", "category": "regional"},
        {"name": "ural_region", "url": "https://www.urfo.org/rss", "kind": "rss", "category": "regional"},
    ],
    "ПЕРМСКИЙ": [
        {"name": "ural_perm_bc", "url": "https://www.business-class.su/rss", "kind": "rss", "category": "regional"},
    ],
    "ТЮМЕНСКАЯ": [
        {"name": "ural_tyumen_vsluh", "url": "https://vsluh.ru/rss/", "kind": "rss", "category": "regional"},
    ],
    "ЧЕЛЯБИНСКАЯ": [
        {"name": "ural_chel_dostup", "url": "https://dostup1.ru/rss/", "kind": "rss", "category": "regional"},
    ],
    "ТАТАРСТАН": [
        {"name": "pfo_tatar_inform", "url": "https://www.tatar-inform.ru/rss", "kind": "rss", "category": "regional"},
    ],
    "НИЖЕГОРОДСКАЯ": [
        {"name": "pfo_nn_nta", "url": "https://nta-pfo.ru/rss/", "kind": "rss", "category": "regional"},
        {"name": "pfo_nn_vremya", "url": "https://www.vremyan.ru/rss/news.rss", "kind": "rss", "category": "regional"},
    ],
    "САМАРСКАЯ": [
        {"name": "pfo_samara_63", "url": "https://63.ru/rss", "kind": "rss", "category": "regional"},
    ],
    "БАШКОРТОСТАН": [
        {"name": "pfo_ufa1", "url": "https://ufa1.ru/rss", "kind": "rss", "category": "regional"},
    ],
    "КРАСНОДАРСКИЙ": [
        {"name": "south_kubnews", "url": "https://kubnews.ru/rss/", "kind": "rss", "category": "regional"},
        {"name": "south_krd_93", "url": "https://93.ru/rss", "kind": "rss", "category": "regional"},
    ],
    "РОСТОВСКАЯ": [
        {"name": "south_rnd_161", "url": "https://161.ru/rss", "kind": "rss", "category": "regional"},
    ],
    "ВОРОНЕЖСКАЯ": [
        {"name": "south_abireg", "url": "https://abireg.ru/rss/", "kind": "rss", "category": "regional"},
    ],
    "ПРИМОРСКИЙ": [
        {"name": "dv_eastrussia", "url": "https://www.eastrussia.ru/rss/", "kind": "rss", "category": "regional"},
        {"name": "dv_newsvl", "url": "https://www.newsvl.ru/rss/", "kind": "rss", "category": "regional"},
    ],
    "ХАБАРОВСКИЙ": [
        {"name": "dv_eastrussia", "url": "https://www.eastrussia.ru/rss/", "kind": "rss", "category": "regional"},
    ],
}


@dataclass
class SourceSuggestion:
    """Одна рекомендация нового источника."""

    name: str
    url: str
    kind: str
    category: str
    topics: list[str]
    coverage: int      # сколько компаний/покрытие темы или региона у аудитории
    status: str = "candidate"   # candidate | added | skipped


@dataclass
class SourceDiscoveryResult:
    """Результат поиска новых источников под аудиторию."""

    current_topics: list[str] = field(default_factory=list)      # темы аудитории
    current_regions: list[str] = field(default_factory=list)     # топ-регионы аудитории
    gaps: list[str] = field(default_factory=list)                # темы без покрытия
    suggestions: list[SourceSuggestion] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "current_topics": self.current_topics,
            "current_regions": self.current_regions,
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
                     top_n_industries: int = 8,
                     top_n_regions: int = 16) -> SourceDiscoveryResult:
    """Формирует рекомендации новых источников (отраслевых и региональных) под аудиторию.

    existing_sources — список SourceSpec из реестра (имена уже добавленных).
    """
    res = SourceDiscoveryResult()

    # 1. Темы аудитории из отраслей.
    topic_counts = _topics_from_profile(profile, top_n_industries)
    res.current_topics = [t for t, _ in sorted(topic_counts.items(), key=lambda x: -x[1])]
    res.reasoning.append(
        f"Темы аудитории: {', '.join(res.current_topics) or '(не определены)'}."
    )

    # 2. Регионы концентрации ЦА.
    res.current_regions = [reg for reg, _ in profile.top_regions[:top_n_regions]]
    if res.current_regions:
        res.reasoning.append(
            f"Топ-регионы ЦА: {', '.join(res.current_regions[:8])}."
        )

    existing_names = {s.name for s in existing_sources} if existing_sources else set()

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
                if topic not in suggested[name].topics:
                    suggested[name].topics.append(topic)
                suggested[name].coverage = max(suggested[name].coverage, coverage_by_topic[topic])
                continue
            suggested[name] = SourceSuggestion(
                name=name, url=cand["url"], kind=cand["kind"], category=cand["category"],
                topics=[topic], coverage=coverage_by_topic[topic],
            )

    # 3. Для каждого региона ЦА проверяем региональные источники.
    for reg_name, reg_cnt in profile.top_regions[:top_n_regions]:
        reg_up = reg_name.upper().strip()
        matched_cands: list[dict] = []
        for reg_key, cands in REGIONAL_SOURCES.items():
            if reg_up == reg_key or reg_up.startswith(reg_key + " ") or f" {reg_key}" in reg_up:
                matched_cands.extend(cands)
        for cand in matched_cands:
            name = cand["name"]
            if name in existing_names:
                continue
            tag = f"region:{reg_name}"
            if name in suggested:
                if tag not in suggested[name].topics:
                    suggested[name].topics.append(tag)
                suggested[name].coverage += reg_cnt
                continue
            suggested[name] = SourceSuggestion(
                name=name, url=cand["url"], kind=cand["kind"], category=cand["category"],
                topics=[tag], coverage=reg_cnt,
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
        res.reasoning.append("Все темы и регионы аудитории уже покрыты текущими источниками.")

    return res


def suggest_source_json(profile: AudienceProfile, existing_sources=None,
                        top_n_industries: int = 8) -> dict:
    """Удобный JSON-вывод рекомендаций (для CLI и интеграции)."""
    return discover_sources(profile, existing_sources, top_n_industries).to_dict()
