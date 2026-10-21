# Python-контракт бейслайна ранкера

Этот срез можно импортировать из backend в том же процессе. Он не подключает себя к API автоматически. Вход — EventBundle **v0.1**; новый B6 v0.2 требует отдельной миграции. Это не контракт сырой Article и не контракт Insight аналитика.

## Вызов

```python
from ranking.engine import rank_events, load_rules
from ranking.contracts import FixtureClock, parse_timestamp
from ranking.b6 import load_bundles
from ranking.event_store import EventStore, ingest_bundles
from ranking.profiles import load_json

as_of = parse_timestamp("2026-10-20T18:00:00+03:00")
profiles = load_json("data/examples/personalization/profiles.json")["profiles"]
profile = next(p for p in profiles if p["company_id"] == "DEMO_TRADE")

with EventStore(":memory:") as store:
    ingest_bundles(
        load_bundles("data/examples/personalization/events.jsonl"),
        store=store,
        clock=FixtureClock("2026-10-20T17:00:00+03:00"),
        source_mode="fixture",
    )
    snapshots = store.list_latest(as_of=as_of)
    result = rank_events(
        snapshots, profile, role="cfo", as_of=as_of,
        rules=load_rules(), entity_mapping=None, top_k=5,
    )
```

В рабочем режиме использовать SystemClock и отдельную постоянную базу, а не fixture-часы. Фактическая сигнатура: `rank_events(snapshots, profile, *, role, as_of, rules, entity_mapping=None, top_k=5)`. `snapshots` — список StoredEvent, `as_of` — datetime с часовым поясом. `rules` обязателен.

## Результат

`result` содержит `company_id`, `profile_version`, `user_role`, `as_of`, версии правил, `ranked`, `feed`, `deferred`, `excluded`, `limitations`. Для фронта выбирать **только `result["feed"]`**. Пустая лента — допустимый результат.

Элемент feed/ranked:

```text
{
  "snapshot": {
    "event_id": "...",
    "event_version": 1,
    "payload_sha256": "...",
    "received_at": "...",
    "source_mode": "fixture",
    "ingest_id": "..."
  },
  "relevance": {
    "score": 0.3674,
    "level": "medium",
    "reasons": [...],
    "candidate_channels": [...],
    "feature_ids": [...],
    "claim_ids": [...],
    "evidence_status": "usable",
    "limitations": [...],
    ...
  }
}
```

Полная машиночитаемая схема: [ranking-result.schema.json](../docs/contracts/ranking-result.schema.json). Вложенный Relevance имеет собственную версию 0.1, независимо от версии входного EventBundle.

Для проверки backend-адаптера готов [backend-integration.json](../data/examples/personalization/backend-integration.json): нормализованный synthetic profile, точные stored_events с bundle и настоящий ranking_response, полученный этим кодом. Это тестовая упаковка входов и результата, не дополнительный runtime API.

## Отображение в backend FeedItem

- Score брать из `item["relevance"]["score"]`, без деления на 100. Contribution каждой причины уже нормирован в 0–1; сумма contributions равна score. Не округлять до проверки суммы.
- `reasons` — структурированные объекты, а не список строк. Сохранять reason_id, text, contribution, feature_ids, claim_ids, event_paths, basis.
- В feed **нет** полного event и `features_used`. Найти исходный StoredEvent по snapshot: ID, версия и payload_sha256 должны совпадать; взять `stored.bundle`. Не заменять его последним доступным событием.
- Происхождение признаков получать из `profile["features"][feature_id]`. ML-профиль не совпадает с упрощённой UI-моделью компании.
- В UI `event_fields` явно переложить наши `event_paths`. Не терять ID claims, версии и hashes при преобразовании.
- Дата — `bundle["event"]["first_published_at"]`; неизвестное значение не заменять текущим днём. Тип события вроде policy_rate не равен UI-теме rates: нужен явный mapping.
- Нельзя определять наличие позиции банка по теме события. Этот модуль не генерирует bank_view.
- Не подменять отсутствие обязательного поля значениями score=0/event_id=unknown. Ошибку контракта вернуть явно.

## История, ошибки и запуск

EventStore сохраняет `(event_id,event_version)` и отвергает коллизию разного payload. При выборе последней версии withdrawn не заменяется старой active. Условие as_of учитывает время получения и producer update. Для смены схемы/пространства версий заранее согласовать миграцию базы.

SQLite-соединение создаётся и используется в одном потоке. In-process не означает, что существующий connection можно без подготовки передать в FastAPI threadpool. Репозиторий компаний загружать один раз на worker; не перечитывать CSV на каждый запрос.

Для импорта нужны Python 3.13+, зависимости root pyproject.toml/uv.lock, ranking с configs, docs/contracts. Для демо также нужны синтетические fixtures. При сборке backend-only Docker image эти файлы сами туда не попадут. Не копировать банковские CSV/PDF в образ или Git.

Проверки:

```bash
uv sync --frozen
uv run --frozen python -m unittest discover -s ranking/tests -v
uv run --frozen python -m ranking demo --as-of 2026-10-20T18:00:00+03:00 --output-dir data/processed/personalization/demo
```

Обученной модели и LLM-вызовов здесь нет. Score — эвристический приоритет, не вероятность и не оценка денежного ущерба.
