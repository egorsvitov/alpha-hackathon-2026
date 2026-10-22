# Impact Retriever

Реализован локальный CPU-путь: структурированный профиль компании → сохранённые 384-мерные векторы → hybrid-поиск B6 v0.2 → `ImpactCandidateBatch v1` + технический trace. Бизнес-выводов, legacy `score/feed` и вызовов LLM этот модуль не создаёт.

Подробный результат реализации и фактические измерения: [отчёт](../docs/implementation/company-embeddings-report.md). Утверждённые планы находятся в [company-embeddings](../docs/retriever/company-embeddings/); их текст не переписывался под результат.

## Установка

Из корня монорепозитория, Python 3.13+:

```bash
uv sync --extra embeddings --frozen
.venv/bin/python -m retriever --help
```

CPU-модель: `intfloat/multilingual-e5-small`, закреплённая ревизия `614241f622f53c4eeff9890bdc4f31cfecc418b3`. Encoder проверяет SHA-256 весов/токенизатора. Подготовленные файлы уже есть локально в `ranking/experiments/cpu_news_embeddings/artifacts/model/`; на другом компьютере их нужно отдельно подготовить или перенести. Во время запросов скачивания нет. Обязательного CUDA, vector DB и сервера для этого модуля нет.

## Компании

```bash
.venv/bin/python -m retriever company-embeddings build \
  --companies data/companies/df_Uid_data_hack.csv \
  --cache data/processed/retriever/objects.sqlite \
  --output data/processed/retriever/companies \
  --model-dir ranking/experiments/cpu_news_embeddings/artifacts/model \
  --ids U_100215 U_100017
```

Без `--ids` строится вся база. `--dry-run` считает доступные компании без inference. `resume` требует совпадения source/config/scope. `verify`, `inspect --company-id ...`, `export --destination ...` читают `--snapshot` и `--cache`. Export переносит mapping и все referenced vector objects; после переноса открыть `<destination>/snapshot` с `<destination>/objects.sqlite`.

Базовые виды: `business`, `finance`, `trade_geo`; отсутствующие сведения дают явный omission. `--universal` добавляет контрольный общий вектор. Граф включается отдельно `--graph <payment-store.sqlite> --weights equal|amount_share|sqrt_share`. Default — без графа. `PaymentGraphStore.build()` принимает проверенный manifest источника; запросы соседей используют SQLite-индексы, не читают CSV заново.

## События и запрос

`index` принимает существующий `ranking.event_store.EventStore`; timestamp получения назначает ingestion. CLI не придумывает задним числом receipt:

```bash
.venv/bin/python -m retriever index \
  --events data/processed/retriever/company-embeddings/events.sqlite \
  --cache data/processed/retriever/objects.sqlite \
  --model-dir ranking/experiments/cpu_news_embeddings/artifacts/model \
  --output data/processed/retriever/events-oct22 \
  --as-of 2026-10-22T00:00:00Z \
  --window-start 2026-10-20T00:00:00Z \
  --window-end 2026-10-22T00:00:00Z

.venv/bin/python -m retriever retrieve \
  --index data/processed/retriever/events-oct22 \
  --cache data/processed/retriever/objects.sqlite \
  --snapshot data/processed/retriever/companies \
  --companies data/companies/df_Uid_data_hack.csv \
  --company-id U_100215 --role cfo \
  --output data/processed/retriever/candidates.json
```

In-process: один раз открыть `EventIndex`, `VectorCache`, `CompanyEmbeddingSnapshot`, затем `load_company_queries(...)` и `RetrievalService(index).run(request, company_queries=queries, config=VariantConfig(...))`. `request` содержит профиль, company_id, role, request_id, as_of и окно; query/corpus должны иметь один fingerprint и точное совпадение cutoff. Запрос не запускает encoder. Профиль передаёт вызывающая сторона; полная загрузка CSV в CLI — удобство однократной команды, не рекомендуемый путь на каждый HTTP-запрос.

Полная проверка company snapshot при открытии всей базы заметно дороже одного поиска: держать его открытым на worker. Результат — `candidate_batch` и `trace`, не готовая лента frontend. Текущий старый адаптер backend требует отдельной миграции.

## Гарантии и ограничения

- Immutable snapshot/hash, L2 float32[384], разные ключи `query:`/`passage:`, отказ при переполнении 512 токенов текста компании; длинные утверждения событий делятся на окна с offsets.
- Latest выбирается до active-фильтра; withdrawn/insufficient/future исключаются. `other` и пустая taxonomy разрешены.
- Dense: максимум по chunks события, top50 по view. Fusion: максимум RRF внутри dense, затем сумма dense/BM25/structured; до 250 union, 100 pool, 20 outputs.
- Budget: 8192 по **оценке UTF-8 bytes/4**, не tokenizer конкретного аналитика. Полный B6 события не обрезается; неуместившиеся кандидаты отмечаются в trace. Поэтому обычно возвращается меньше 20.
- В аналитический payment context входят top5 наблюдаемых рёбер каждого направления и coverage; весь графовый контекст доступен в trace по hash/ref. Это ограничение явно обозначено, не доказательство отсутствия других связей.
- Без подтверждённых `available_at` и `period_end` граф не используется в strict-режиме. `static_context` — явно помеченный диагностический эксперимент; не исторический тест.
- Платёжное ребро не доказывает поставщика/покупателя. Сходство не доказывает бизнес-влияние. Условия кредитования не означают наличие конкретной ставки или долга.
- При ошибке канала запрос падает явно. Автоматический degraded-fallback не реализован. Кластеризация/дедупликация разных новостей не добавлялась.

## Проверки и локальные эксперименты

```bash
RETRIEVER_REAL_CPU_TEST=1 .venv/bin/python -m unittest discover -s retriever/tests -v
.venv/bin/python -m unittest ranking.tests.test_profiles ranking.tests.test_graph ranking.tests.test_contracts ranking.tests.test_b6 ranking.tests.test_event_store -v
```

Без `RETRIEVER_REAL_CPU_TEST=1` реальная модель не нужна: соответствующий тест пропускается, остальные используют синтетический encoder.

Воспроизводимый локальный прогон на уже сохранённых данных (никаких платных API):

```bash
.venv/bin/python -m retriever.evaluation.embeddings.smoke smoke
.venv/bin/python -m retriever.evaluation.embeddings.smoke pilot
.venv/bin/python -m retriever.evaluation.embeddings.graph_pilot
.venv/bin/python -m retriever.evaluation.embeddings.smoke full
```

Следующие стадии проверяют предыдущий gate. Изменившиеся источники или код требуют отдельной директории эксперимента, а не перезаписи immutable индекса. Размеченные judgments скрипт не перезаписывает.

Артефакты исключены из Git: `data/processed/retriever/company-embeddings/`. Веса и сырые датасеты не добавлять в коммит. [Оценка качества и форма разметки](evaluation/embeddings/README.md).
