# Handoff для команды ML (Булат, Егор, Павел): Интеграция с Backend и Frontend

Этот документ описывает, как результаты работы ваших модулей (`news/`, `llm/events/`, `ranking/`) стыкуются с `backend/` и отображаются в интерфейсе интернет-банка у Макса.

---

## 1. Архитектурная цепочка данных

```text
[news/collect.py] 
       │  (Сырые статьи Article: text, source, url, title, published_at)
       ▼
[llm/events/ (B2 -> B5 -> B6)]
       │  (NLP, извлечение утверждений, нормализация, EventBundle v0.2)
       ▼
[ranking/]
       │  (Снапшоты событий + профили компаний -> rank_events -> RankedEvents / feed)
       ▼
[backend/ (FastAPI + ranking_adapter.py + ml_bridge.py)]
       │  (Нормализация в DTO FeedItem и Insight, сохранение provenance и трассировки)
       ▼
[frontend/]
          (Главный экран кабинета Альфа-Бизнес, виджет ленты, детальная карточка, чат)
```

---

## 2. Где лежит код бэкенда и как он работает

Основной код бэкенда находится в директории `backend/`:

* `backend/app/schemas/base.py` — Pydantic-модели, которые ждет фронтенд:
  * `CompanyProfile` — профиль компании (выручка, ОКВЭД, флаги продуктов, ВЭД/ФТС, контрагенты);
  * `FeedItem` — элемент ленты (событие `NewsEvent`, скоринг `Relevance`, факторы влияния `RelevanceFactor`);
  * `Insight` — детальная карточка инсайта (факты, позиция банка, риски, действия ЛПР, источники `Evidence`);
  * `QaAnswer` — ответ на уточняющий вопрос ЛПР.
* `backend/app/services/ranking_adapter.py` — адаптер ранкера Павла:
  * Принимает результат `ranking.engine.rank_events(...)`;
  * Преобразует `feed` в `list[FeedItem]` для фронтенда;
  * **Строго сохраняет трассировку**: поля `feature_ids`, `event_paths`, `basis` и полный словарь `provenance` перекладываются в `RelevanceFactor`;
  * Проверяет контракт B6: если приходит B6 v0.2 без соответствующего обновления ранкера, выбрасывает явное исключение `UnsupportedContractVersionError`.
* `backend/app/services/ml_bridge.py` — сервисный мост для подхвата ваших артефактов `EventBundle`:
  * Автоматически проверяет наличие опубликованных бандлов в `data/processed/events/bundles.v0.2.jsonl` или `active.v0.2.jsonl`;
  * Конвертирует бандлы в `NewsEvent`, `Claim` и `Evidence`.
* `backend/test_e2e.py` — сквозной тест всей системы.

---

## 3. Как проверить сквозную работу с бэкендом

1. Активируйте окружение и запустите E2E-тест:
   ```bash
   python backend/test_e2e.py
   ```
   Скрипт проверяет 8 сценариев (выбор компании, лента под CFO/CEO, карточка инсайта, чат, загрузка новости, фидбек, метрики).

2. Запуск бэкенда для тестирования в браузере с фронтендом Макса:
   ```bash
   cd backend
   pip install -r requirements.txt
   uvicorn main:app --reload --port 8000
   ```
   Swagger UI доступен по адресу: `http://localhost:8000/docs`.

---

## 4. Что ожидает бэкенд от ваших модулей при финализации

### Для Егора и Булата (модуль `llm/events/`):
* Ваши опубликованные события B6 экспортируются командой:
  ```bash
  python -m llm.events.bundle_cli --catalog news export --output data/processed/events/bundles.v0.2.jsonl
  ```
* Модуль `backend/app/services/ml_bridge.py` уже умеет подхватывать этот файл автоматически.

### Для Павла (модуль `ranking/`):
* Ранкер вызывается напрямую через Python in-process:
  ```python
  from ranking.engine import rank_events, load_rules
  result = rank_events(snapshots, profile, role=role, as_of=as_of, rules=rules)
  ```
* Результат передается в наш адаптер:
  ```python
  from app.services.ranking_adapter import adapt_ranking_response_to_feed
  feed_items = adapt_ranking_response_to_feed(result, stored_events, profile, profile_version)
  ```
  И моментально отдается в эндпоинт `GET /api/v1/companies/{id}/news-feed`.

---
