#!/usr/bin/env python3
"""Smoke-тест пайплайна без сети: фейковый fetcher → JSONL, фильтр шума и региональный профиль ЦА.

Проверяет:
  1. Реестр источников (news/sources.json) валиден и включает региональные СМИ.
  2. Кандидаты превращаются в Article, дедупликация по URL работает, формат корректен.
  3. HTML-фильтр отсекает статику/навигацию и пропускает новости Минфина.
  4. Кэш Fetcher не замораживает RSS-ленты, но кэширует скачанные статьи.
  5. Фильтр качества (news/core/quality.py) отсекает технический биржевой мусор Мосбиржи,
     таблицы/реестры ЦБ и бытовой таблоид, СОХРАНЯЯ все реальные финансовые показатели.
  6. Очистка текста (clean_article_text) убирает ведущие URL, микроразметку и дубли 1prime.
  7. Региональный профиль ЦА (batch_plan, topic_filter, source_discovery) корректно
     выделяет региональные кластеры и фильтрует региональные деловые новости.

Запуск из корня проекта:  python3 news/smoke_test.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from news.core.fetcher import Fetcher  # noqa: E402
from news.core.model import ArticleCandidate  # noqa: E402
from news.core.pipeline import NewsPipeline  # noqa: E402
from news.core.quality import (  # noqa: E402
    clean_article_text,
    is_junk_text,
    is_noise_article,
    is_noise_candidate,
)
from news.core.registry import SourceSpec, load_registry  # noqa: E402
from news.core.store import JsonlStore  # noqa: E402
from news.profile.audience import AudienceProfile  # noqa: E402
from news.profile.batch_plan import build_batch_plan  # noqa: E402
from news.profile.source_discovery import discover_sources  # noqa: E402
from news.profile.topic_filter import TopicFilter  # noqa: E402


class FakeFetcher:
    def fetch_candidates(self, spec: SourceSpec):
        return [
            ArticleCandidate(url="https://example.com/a", title="Статья A"),
            ArticleCandidate(url="https://example.com/b", title="Статья B"),
            ArticleCandidate(url="https://example.com/a", title="Статья A (дубль)"),
        ]

    def fetch_article(self, candidate: ArticleCandidate):
        return type("R", (), {"text": "Текст " + candidate.title, "title": candidate.title,
                              "published_at": None, "error": None})()


def main() -> int:
    registry = load_registry("news/sources.json")
    if not registry.sources:
        print("FAIL: реестр пуст")
        return 1

    # Проверяем, что региональная категория присутствует в реестре.
    regional_sources = registry.enabled(["regional"])
    assert len(regional_sources) >= 10, f"ожидали >=10 региональных источников, есть {len(regional_sources)}"

    with tempfile.TemporaryDirectory() as tmp:
        store = JsonlStore(tmp)
        pipe = NewsPipeline(registry, FakeFetcher(), store)
        # Запускаем только первый источник (cbr_rss).
        first = registry.sources[0]
        spec_reg = type("R", (), {"enabled": lambda self, c=None: [first], "sources": []})()

        # Вручную: эмулируем один проход через run с подменой реестра.
        pipe.registry = spec_reg
        report = pipe.run()
        assert report.articles_written == 2, f"ожидали дедуп до 2, получили {report.articles_written}"

        lines = store.corpus_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2, f"в файле должно быть 2 строки, есть {len(lines)}"
        art = json.loads(lines[0])
        for field in ("article_id", "source", "url", "title", "published_at", "language", "text"):
            assert field in art, f"нет поля {field}"
        assert art["text"].startswith("Текст"), "текст не извлечён"
        assert art["source"] == first.name

    # HTML-фильтр новостей: мусор (favicon, css, навигация, фильтры) отсекается,
    # настоящие статьи Минфина (id_4=digits-slug) проходят.
    news = "https://minfin.gov.ru/ru/press-center/?id_4=40706-gosduma_utverdila_kandidaturu"
    assert Fetcher._is_news_link(news), "реальная новость Минфина должна проходить"
    for junk in (
        "https://minfin.gov.ru/common/core/fav/favicon.ico",
        "https://minfin.gov.ru/common/core/css/text.css",
        "https://minfin.gov.ru/ru/ministry/",
        "https://minfin.gov.ru/ru/press-center/?mode_4=default&amp;TYPE_ID_4=2",
        "https://example.com/img/logo.png",
    ):
        assert not Fetcher._is_news_link(junk), f"мусор не должен проходить: {junk}"
    title = Fetcher._title_from_html_link(news)
    assert "gosduma" in title, f"заголовок из slug не извлечён: {title!r}"

    # Проверка кэша Fetcher: ленты (rss) всегда запрашиваются заново по сети,
    # а статьи (article) берутся из кэша без повторного сетевого вызова.
    with tempfile.TemporaryDirectory() as tmp_cache:
        calls: list[str] = []

        class DummyResp:
            def __init__(self, text: str):
                self.text = text

            def raise_for_status(self) -> None:
                pass

        class DummySession:
            headers: dict[str, str] = {}

            def get(self, url: str, timeout: float = 20.0) -> DummyResp:
                calls.append(url)
                return DummyResp(f"attempt-{len(calls)}")

        f = Fetcher(cache_dir=tmp_cache, delay=0.0, session=DummySession())  # type: ignore[arg-type]
        assert f._http_get("https://example.com/rss", "rss") == "attempt-1"
        assert f._http_get("https://example.com/rss", "rss") == "attempt-2", "RSS не должен залипать в кэше"
        assert f._http_get("https://example.com/art1", "article") == "attempt-3"
        assert f._http_get("https://example.com/art1", "article") == "attempt-3", "Статья должна браться из кэша"
        assert len(calls) == 3

    # Проверка фильтра технического и бытового шума (news/core/quality.py):
    # 1. Технический шум Мосбиржи и таблиц ЦБ отсекается:
    for tech_url, tech_title in (
        ("https://www.moex.com/n94635", "О допуске ценных бумаг к операциям РЕПО с 23 октября"),
        ("https://www.moex.com/n94636", "В Системе торгов изменены значения верхней границы ценового коридора"),
        ("https://www.moex.com/n94637", "По ценным бумагам ПАО проводятся дискретный аукцион"),
        ("https://www.moex.com/n94638", "Технические работы на тестовых полигонах фондового и валютного рынков"),
        ("https://www.cbr.ru/statistics/macro_itm/external_sector/ir/", "Статистика внешнего сектора"),
        ("https://www.cbr.ru/finorg/foinfo/reports/?ogrn=10277", "Отчетность финансовой организации"),
        ("https://lenta.ru/news/2026/10/23/dtp/", "В ночном ДТП на трассе столкнулись легковушки"),
        ("https://93.ru/text/sport/2026/10/23/123/", "Футболист забил победный гол в матче чемпионата России"),
    ):
        assert is_noise_candidate(tech_url, tech_title), f"должен отсекаться технический/бытовой шум: {tech_title}"

    # 2. Финансовые показатели, макроэкономика, ВЭД и региональные деловые новости СОХРАНЯЮТСЯ:
    for good_url, good_title in (
        ("https://www.moex.com/n94500", "Объем размещений облигаций на Московской бирже вырос на 67% — до 2,4 трлн рублей"),
        ("https://www.cbr.ru/press/event/?id=32927", "Банк России сохранил ключевую ставку на уровне 18% годовых"),
        ("https://infopro54.ru/news/1", "Выручка промышленных предприятий Новосибирской области выросла на 14%"),
        ("https://www.business-class.su/news/1", "В Пермском крае построят логистический комплекс за 5,2 млрд рублей"),
        ("https://www.tatar-inform.ru/news/1", "Экспорт продукции АПК Татарстана в Китай увеличился на 22%"),
    ):
        assert not is_noise_candidate(good_url, good_title), f"реальная деловая новость не должна отсекаться: {good_title}"

    # 3. Очистка текста 1prime от ведущего URL, микроразметки и дублей заголовка:
    dirty_prime = (
        "https://1prime.ru/20261022/minselhoz-874520558.html\n"
        "Минсельхоз рассчитывает довести долю российского вина на рынке до 80%\n"
        "Минсельхоз рассчитывает довести долю российского вина на рынке до 80% - 22.10.2026, ПРАЙМ\n"
        "Минсельхоз рассчитывает довести долю российского вина на рынке до 80%\n"
        "2026-10-22T15:29+0300\n"
        "https://img.cdn1prime.ru/images/sharing/article/rus/874520558.jpg\n"
        "ГЕЛЕНДЖИК, 22 окт - ПРАЙМ. Минсельхоз рассчитывает довести долю российского вина на рынке до 80% "
        "в ближайшие 7-10 лет, объем инвестиций в отрасль превысит 35 млрд рублей.\n"
        "Подписывайтесь на наш канал в Telegram"
    )
    cleaned_prime = clean_article_text(
        dirty_prime,
        title="Минсельхоз рассчитывает довести долю российского вина на рынке до 80% - 22.10.2026, ПРАЙМ",
        url="https://1prime.ru/20261022/minselhoz-874520558.html",
    )
    assert cleaned_prime.startswith("ГЕЛЕНДЖИК, 22 окт - ПРАЙМ."), f"шапка 1prime не очищена: {cleaned_prime!r}"
    assert "35 млрд рублей" in cleaned_prime, "финансовые показатели должны сохраняться в тексте"
    assert "Подписывайтесь" not in cleaned_prime, "хвостовая реклама Telegram должна удаляться"
    assert not is_noise_article(
        "https://1prime.ru/20261022/minselhoz-874520558.html",
        "Минсельхоз рассчитывает довести долю российского вина на рынке до 80%",
        cleaned_prime,
        source="1prime",
    )

    # 4. Навигационная заглушка ЦБ распознаётся как мусор:
    cbr_nav = "107016, Москва, ул. Неглинная, д. 12, к. В, Банк России\n8 800 300-30-00\nwww.cbr.ru\nПоиск по сайту"
    assert is_junk_text(cbr_nav), "навигационная заглушка ЦБ должна отсекаться"

    # 5. Региональный профиль ЦА, план батча, TopicFilter и SourceDiscovery:
    prof = AudienceProfile(
        total_companies=85525,
        active_clients=21820,
        prospects=61257,
        top_industries=[
            ("Аренда и управление собственным или арендованным недвижимым имуществом", 9960),
            ("Строительство жилых и нежилых зданий", 3294),
            ("Торговля оптовая неспециализированная", 2149),
        ],
        top_regions=[
            ("МОСКВА", 19866),
            ("САНКТ-ПЕТЕРБУРГ", 10724),
            ("НОВОСИБИРСКАЯ", 3908),
            ("СВЕРДЛОВСКАЯ", 2953),
            ("ТАТАРСТАН", 2387),
        ],
        top_trade_countries=[("КИТАЙ", 3711), ("ТУРЦИЯ", 522)],
        fts_share=0.068,
    )
    plan = build_batch_plan(prof, registry.sources)
    assert "regional" in plan.categories, "план батча должен включать региональную категорию"
    assert "НОВОСИБИРСКАЯ" in plan.top_regions
    tf = TopicFilter(plan)
    assert tf.matches("В Новосибирской области запустили новый логистический терминал")
    assert tf.matches("В Екатеринбурге обсудили льготы для застройщиков на Среднем Урале")
    disc = discover_sources(prof, existing_sources=[])
    assert any(s.category == "regional" for s in disc.suggestions), "discovery должен рекомендовать региональные СМИ"

    print("smoke test OK: реестр валиден, дедуп работает, фильтр технического шума и региональный профиль ЦА корректны")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
