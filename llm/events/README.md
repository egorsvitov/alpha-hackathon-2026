# Новости → экономические события

Модуль получает тексты новостей, извлекает утверждения, проверяет их основания,
сопоставляет события и публикует **EventBundle v0.2**. Тексты и результаты обработки
сохраняются с версиями. Анализ влияния на конкретную компанию выполняется отдельно.

Начать со [схемы пайплайна](docs/pipeline.md). Точка остановки экспериментов и
нерешённые проблемы — [checkpoint](../../context/news-pipeline-checkpoint.md).

Новые ручные выгрузки готовятся командой `corpus`: [переход к разбору групп](docs/cluster-first.md).
Эта часть пока включает подготовку корпуса и поиск соседей; новая LLM-часть ещё не реализована.

## Быстрая проверка без API

Из корня репозитория, Python 3.13+:

```bash
python3 -m venv .venv_events
.venv_events/bin/python -m pip install -r llm/events/requirements.lock
.venv_events/bin/python -m llm.events check
.venv_events/bin/python -m unittest discover -s llm/events/tests
.venv_events/bin/python -m llm.events demo --output data/processed/events/demo-clean
```

`demo` проходит извлечение, B4, B5 и B6 на синтетической статье с заранее заданными
ответами провайдера. На выходе — `report/bundles.json`, вход и диагностика.
Это проверка соединения блоков, а не качества модели. API и ключ не нужны.
Повторный запуск использует кэш.

## Где искать код

| Папка | Что делает | Основной API |
|---|---|---|
| `corpus/` | Ручные экспорты, аудит и группы для разметки | `prepare_corpus`, `candidate_neighborhoods` |
| `contracts/` | B1: модели, цитаты, связи, разметка | `validate_bundle`, `validate_grounding` |
| `storage/` | B2: батчи, неизменяемые редакции, задания | `EventStore` |
| `extraction/` | B3: запросы, пакеты, проверка B, бюджет | `ExtractionService` |
| `normalization/` | B4: числа, даты, единицы, организации | `NormalizationService` |
| `catalog/` | B5: идентичность событий, конфликты, история | `CatalogService` |
| `publication/` | B6: метки, качество, версии и JSONL | `EventBundleService` |
| `tests/`, `fixtures/` | Офлайн-проверки и синтетические примеры | `unittest` |
| `profiles/` | Примеры настроек без секретов | `ProviderProfile`, `ExecutionLimits` |

Внутри стадии `models.py` описывает её данные, `service.py` работает с хранилищем,
`cli.py` даёт команды. Чистые функции находятся в `core.py`, `staged.py`, `matching.py`
или `features.py`; они сами не выполняют API-вызовы. Подробности — в
[карте архитектуры](docs/structure.md).

## Команды

Одна точка входа: `python -m llm.events`. У каждой команды есть `--help`.

```bash
python -m llm.events --help
python -m llm.events storage --help
python -m llm.events extract --help
python -m llm.events normalize --help
python -m llm.events catalog --help
python -m llm.events publish --help
```

| Команда | Назначение | Подробности |
|---|---|---|
| `import-news <file.jsonl> --db <db>` | Подготовить JSONL сборщика и импортировать в B2 | [Пайплайн](docs/pipeline.md) |
| `storage` | Импорт JSON-массива, версии и задания | [B2](docs/storage.md) |
| `extract` | Discovery, план пакетов, A/B, отчёт | [B3](docs/extraction.md) |
| `llm` | Простой B3 b3.1, профили, журнал расхода | [B3](docs/extraction.md) |
| `normalize` | Преобразование выбранного B3-артефакта | [B4](docs/normalization.md) |
| `catalog` | Выбор результатов B4 и сопоставление | [B5](docs/catalog.md) |
| `publish` | Публикация B6, чтение и экспорт | [B6](docs/publication.md) |
| `check` | Все схемы и синтетические контрактные примеры | Без сети |
| `contracts` | Проверить собственную разметку через `--gold/--articles` | [Разметка](docs/annotation.md) |

Параметры `--db`, `--catalog`, `--profile` у стадий обычно стоят **до** их подкоманды.
У `llm` профиль и БД указываются после подкоманды; точный синтаксис показывает `--help`.

## Данные и версии

По умолчанию SQLite находится в `data/processed/events/state.sqlite3`.
Все локальные корпуса, ответы API, отчёты и выгрузки — в игнорируемом `data/`.
В Git остаются код, схемы и синтетические примеры. Ключ — `LLM_API_KEY` в окружении
или локальном `.env`; загрузка `.env` выполняется только сетевым CLI.

Новая редакция статьи создаёт новую версию, старые цитаты сохраняются. Каталог B5 и
публикация B6 имеют собственные версии. Изменение статьи делает зависимые каталоги
устаревшими; чтение не запускает обработку автоматически. SQLite-схема остаётся 4.

## Для потребителя B6

[Внешний контракт](../../docs/b6-event-contract.md) и
[семь примеров](../../docs/b6-examples/v0.2) не изменены.

```python
from llm.events import EventStore, EventBundleService, validate_bundle

service = EventBundleService(EventStore(), catalog_id="news")
for payload in service.export_events():  # опубликованный актуальный снимок
    bundle = validate_bundle(payload)
    if bundle.event.availability == "withdrawn" or bundle.event.quality == "insufficient":
        continue
    cache_key = (bundle.event.event_id, bundle.event.event_version)
    # Передать bundle ранжировщику, учитывая conflicts и limitations.
```

Проверка цитат требует архивные `ArticleRecord`: `validate_grounding(payload, records)`.
Она не доказывает истинность новости или смысловую поддержку каждого поля.
Пример чтения JSONL: `python -m llm.events.examples.read_bundles <events.jsonl>`.
Ранжировщик/backend пока не подключены к producer автоматически.

## После уборки

Оставлены простой B3 b3.1 и последняя пакетная схема `two-stage.v2` с `span-ids.v1`.
Исполняемые `compact/form/rows`, первая двухэтапная схема и независимый `legacy`
удалены. Старые результаты остаются в SQLite как данные; старые форматы не запускаются.
Импорты подмодулей изменились: например, `llm.events.storage` →
`llm.events.storage.store`, `llm.events.extraction_v2_store` →
`llm.events.extraction.service`. Основные импорты из `llm.events` сохранены.

После переноса реализация получает новые хэши: старые сохранённые планы не следует
возобновлять новым кодом, нужно явно построить новые. Исторические bundles не
пересчитываются. Полный список решений — [карта уборки](docs/structure.md).
