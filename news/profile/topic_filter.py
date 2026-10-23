"""Тематический фильтр кандидатов по плану батча (из профиля аудитории).

Позволяет собирать не всё подряд, а только релевантные целевой аудитории
новости: отбирает кандидатов, чей заголовок/описание затрагивает темы,
отрасли, страны ВЭД или регионы концентрации ЦА из BatchPlan.

Фильтрация лёгкая (по ключевым словам, без LLM), поэтому не тратит бюджет и
может выполняться на этапе сбора. Не отбрасывает финансовые показатели, а
сокращает поток до релевантных отраслей, регионов и макроэкономики.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from news.profile.batch_plan import BatchPlan

# Ключевые слова для тем (расширяется по мере добавления отраслей).
TOPIC_KEYWORDS: dict[str, list[str]] = {
    "realestate": ["недвижим", "жил", "офис", "строй", "аренд", "ипотек", "девелоп"],
    "construction": ["строител", "застройщ", "подряд", "жкх", "смет", "долгострой", "инфраструктур"],
    "trade": ["торговл", "ритейл", "ретейл", "импорт", "экспорт", "поставк", "маркетплейс"],
    "hospitality": ["ресторан", "гостиниц", "общепит", "туризм", "отель", "кафе"],
    "it": ["программн", "софт", "разработ", "цифров", "ии", "нейросет", "it", "стартап", "технопарк"],
    "logistics": ["перевоз", "логистик", "груз", "транспорт", "порт", "судоход", "склад", "транзит"],
    "finance": ["банк", "кредит", "ставк", "финанс", "вклад", "ликвидност", "лизинг", "факторинг", "выручк", "прибыл"],
    "rates": ["ключевую ставк", "ставк", "цб", "центробанк", "денежно-кредит"],
    "inflation": ["инфляц", "цены выросли", "потребительск", "темп роста цен"],
    "currency": ["курс рубл", "валюта", "доллар", "евро", "рубль", "юан"],
    "sanctions": ["санкци", "ограничени", "эмбарго", "санкцион"],
    "regulation": ["регулирован", "закон", "постановлен", "минфин", "правительств", "налог", "ндс", "пошлин", "тариф", "субсид"],
    "oil_gas": ["нефт", "газ", "трубопровод", "топлив"],
    "metallurgy": ["металл", "стал", "прокат", "алюмин"],
    "agro": ["сельск", "зерно", "урожай", "аграр", "апк"],
    "pharma": ["фармацевт", "лекарств", "медпрепар", "клиник", "медицин"],
    "marketing": ["реклам", "маркетинг", "продвиж"],
    "rnd": ["исследовани", "научн", "разработк"],
}

# Имена стран (нормализованные, для регистрационных новостей о ВЭД).
COUNTRY_ALIASES: dict[str, list[str]] = {
    "КИТАЙ": ["кита", "хэнань", "шанхай", "пекин", "китайск"],
    "ТУРЦИЯ": ["турц", "турецк", "стамбул"],
    "ИНДИЯ": ["инди"],
    "ОАЭ": ["оаэ", "дубай", "эмират"],
    "УЗБЕКИСТАН": ["узбек", "ташкент"],
    "ГОНКОНГ": ["гонконг"],
    "ЮЖНАЯ КОРЕЯ": ["коре", "сеул"],
    "КАЗАХСТАН": ["казах", "астан", "алмат"],
    "БЕЛАРУСЬ": ["белорус", "беларус", "минск"],
    "ГЕРМАНИЯ": ["герман", "немец"],
}

# Алиасы ключевых регионов концентрации ЦА (по полю REGION из df_Uid_data_hack.csv).
REGION_ALIASES: dict[str, list[str]] = {
    "МОСКВА": ["москв", "столиц", "мэр москвы"],
    "МОСКОВСКАЯ": ["подмосков", "московск"],
    "САНКТ-ПЕТЕРБУРГ": ["петербург", "спб", "петербурж", "смольн"],
    "ЛЕНИНГРАДСКАЯ": ["ленинградск", "ленобл"],
    "НОВОСИБИРСКАЯ": ["новосибирск", "нсо", "сибсельмаш", "академгород"],
    "СВЕРДЛОВСКАЯ": ["екатеринбург", "свердловск", "среднем урале", "урал"],
    "КРАСНОДАРСКИЙ": ["краснодар", "кубан", "сочи", "новороссийск"],
    "ТАТАРСТАН": ["татарстан", "казан", "челн", "нижнекамск", "алабуг"],
    "НИЖЕГОРОДСКАЯ": ["нижегород", "нижнем новгороде", "нижний новгород", "дзержинск"],
    "ЧЕЛЯБИНСКАЯ": ["челябинск", "магнитогорск", "южном урале"],
    "САМАРСКАЯ": ["самар", "тольятти"],
    "ПЕРМСКИЙ": ["перм", "прикамь"],
    "ТЮМЕНСКАЯ": ["тюмен", "тобольск"],
    "РОСТОВСКАЯ": ["ростов", "донск", "таганрог"],
    "БАШКОРТОСТАН": ["башкортостан", "башкир", "уфа", "уфе", "уфимск"],
    "КРАСНОЯРСКИЙ": ["красноярск", "норильск", "енисей"],
    "ПРИМОРСКИЙ": ["примор", "владивосток", "находк", "дальнем востоке", "дфо"],
    "ВОРОНЕЖСКАЯ": ["воронеж", "чернозем"],
    "АЛТАЙСКИЙ": ["алтай", "барнаул"],
    "ОМСКАЯ": ["омск"],
    "ХАБАРОВСКИЙ": ["хабаровск", "комсомольск"],
    "ИРКУТСКАЯ": ["иркутск", "приангарь", "байкал"],
}


@dataclass
class TopicFilter:
    """Фильтр кандидатов по темам/странам/регионам плана батча."""

    plan: BatchPlan
    include_topics: bool = True
    include_countries: bool = True
    include_regions: bool = True
    # Безусловно релевантные макро-темы: важны всем компаниям независимо от отрасли.
    # Если новость затрагивает любую из них — пропускаем её всегда.
    always_topics: tuple[str, ...] = (
        "finance", "rates", "inflation", "currency", "sanctions", "regulation",
    )

    def _compile_patterns(self) -> tuple[list[str], list[str], list[str], list[str]]:
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
        region_pats: list[str] = []
        for reg in self.plan.top_regions:
            reg_up = reg.upper().strip()
            for reg_key, aliases in REGION_ALIASES.items():
                if reg_up == reg_key or reg_up.startswith(reg_key + " ") or f" {reg_key}" in reg_up:
                    for alias in aliases:
                        region_pats.append(re.escape(alias))
        return topic_pats, country_pats, region_pats, always_pats

    def matches(self, title: str, summary: str = "") -> bool:
        """True, если кандидат релевантен плану (тема, страна, регион ИЛИ макро-тема)."""
        text = f"{title} {summary}".lower()
        topic_pats, country_pats, region_pats, always_pats = self._compile_patterns()
        # Макро-темы (ставка, инфляция, санкции, регуляторика, финансы) — всегда релевантны.
        if always_pats and any(p in text for p in always_pats):
            return True
        if self.include_topics and topic_pats:
            if any(p in text for p in topic_pats):
                return True
        if self.include_countries and country_pats:
            if any(p in text for p in country_pats):
                return True
        if self.include_regions and region_pats:
            if any(p in text for p in region_pats):
                return True
        # Ничего не задано — пропускаем всё (не фильтруем).
        return not topic_pats and not country_pats and not region_pats

    def filter_candidates(self, candidates) -> list:
        """Отбирает кандидатов, релевантных плану."""
        out = []
        for c in candidates:
            summary = getattr(c, "summary", "") or ""
            if self.matches(c.title, summary):
                out.append(c)
        return out
