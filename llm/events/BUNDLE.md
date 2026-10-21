# B6: публикация и экспорт EventBundle v0.2

B6 преобразует одну согласованную ревизию каталога B5 в неизменяемую публикацию
EventBundle. Работает без сети и LLM, используя проверенные утверждения B4/B5
и архив точных редакций статей. JSON Schema и примеры v0.1/v0.2 не изменены.

## Подготовка и запуск

Python 3.13+, зависимости — `llm/events/requirements.lock`. Из корня проекта:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r llm/events/requirements.lock
```

Для существующей NLP-базы схемы 1–3 нужна явная миграция. Каждый шаг создаёт
отдельную SQLite-backup с правами 0600; существующая копия не перезаписывается.
`--backup <path>` задаёт путь первой копии. Рабочую базу перед открытием обновить
до схемы 4. `migrate_v2_to_v3()` сохраняет прежний точный целевой номер 3.

```bash
.venv/bin/python -m llm.events.bundle_cli --db data/processed/events/state.sqlite3 migrate
```

Для новой базы миграция не нужна: B2 создаёт схему 4. Подготовить статьи и
успешные B4-артефакты по [STORAGE.md](STORAGE.md), [EXTRACTION.md](EXTRACTION.md)
и [NORMALIZATION.md](NORMALIZATION.md), затем явно выбрать их в B5:

```bash
.venv/bin/python -m llm.events.catalog_cli --catalog news init
.venv/bin/python -m llm.events.catalog_cli --catalog news apply --artifact-id <b4_artifact_id>
.venv/bin/python -m llm.events.catalog_cli --catalog news events
.venv/bin/python -m llm.events.bundle_cli --catalog news publish
.venv/bin/python -m llm.events.bundle_cli --catalog news get <event_id>
.venv/bin/python -m llm.events.bundle_cli --catalog news export --output data/processed/events/bundles.v0.2.jsonl
```

`--db` и `--catalog` — глобальные параметры перед подкомандой. По умолчанию
используется `data/processed/events/state.sqlite3`, каталог `default`.
CLI B6 требует существующую базу и не создаёт её при опечатке в пути.
Пустой, но опубликованный каталог экспортируется в пустой JSONL.
Родительский каталог выходного файла должен существовать.

## Python API

```python
from llm.events import EventStore, EventBundleService

service = EventBundleService(EventStore(), catalog_id="news")
receipt = service.publish()
bundle = service.get_event(event_id)  # обычный JSON-совместимый dict, schema_version="0.2"
receipt = service.export_events("data/processed/events/bundles.v0.2.jsonl")
```

В отличие от него, `CatalogService.get_event()` возвращает внутренний B5 record.
B6 `get_event` и `export_events` ничего не записывают в таблицы базы и не создают
версий. Связи на архивные claims и соседние события не загружаются рекурсивно.
Исторический API и выбор `as_of` на этом этапе не предоставляются.

Экспорт — UTF-8 JSONL, один bundle на строку, сортировка по `event_id`.
Отозванные события включены по умолчанию, чтобы потребитель снял прежний
результат. Для отдельной выгрузки только активных событий:

```bash
.venv/bin/python -m llm.events.bundle_cli --catalog news export --output data/processed/events/active.v0.2.jsonl --active-only
```

Эквивалент Python — `export_events(path, include_withdrawn=False)`.
Сначала читается одна публикация, затем файл записывается через временный файл
в том же каталоге и атомарную замену. Ошибка записи сохраняет прежний файл.
Выходной файл нельзя направить на саму SQLite-базу или её WAL/SHM.

## Версии и актуальность

Публикация хранит ревизию и поколение каталога, правила B6 и соответствие версий
B5/B6. `event_id` общий, **номера event_version B5 и B6 сравнивать нельзя**.
Первая версия B6 — 1. Изменение выдаваемых полей повышает её; изменение только
внутреннего аудита B5 может создать новую публикацию без новой версии bundle.
`updated_at` — момент подготовки сохранённой версии B6, а не время чтения.

Точный повтор `publish()` возвращает `cached`. Изменение правил проекции или
её зависимостей обнаруживается по хэшу файлов. Чтобы явно принять новые правила:

```bash
.venv/bin/python -m llm.events.bundle_cli --catalog news publish --rebuild
```

Это не пересборка B5. Неизменившийся payload сохраняет прежнюю версию и timestamp
даже после принятия новых правил. Публикация всего снимка атомарна; гонка с B5,
изменением источников или другой публикацией даёт `catalog_changed`.

После новой редакции статьи: пересобрать B5, явно применить готовый B4 новой
редакции, затем снова опубликовать B6. Допустимо сначала пересобрать и опубликовать
каталог без отсутствующего B4: ограничения и withdrawn будут переданы явно.
После `review`, `revoke`, `apply` или изменения ревизии через `rebuild` также
повторить B6 `publish`.

| Ошибка | Действие |
|---|---|
| `storage_migration_required` | Выполнить явную миграцию до схемы 4 |
| `catalog_stale` | Выполнить B5 rebuild/apply |
| `bundle_not_published` | Выполнить B6 publish |
| `b6_stale` | Опубликовать обновлённую ревизию B5 |
| `bundle_configuration_mismatch` | Явно принять правила через publish --rebuild |
| `catalog_changed` | Повторить публикацию по новому согласованному состоянию |
| `event_not_found` | Проверить ID; пустой успешный bundle не возвращается |

Ошибки CLI имеют `issues: [{code, path, message}]`. Коды завершения:
0 — успех, 1 — ошибка операции, 2 — ошибка аргументов.

## Качество и происхождение

Claims, sources, evidence, conflicts и relations сохраняются без преобразования
содержимого. Перед публикацией проверяются ссылки, схема и точные Unicode-срезы
архивных текстов. `scenario`, `value_role`, отрицание, модальность и периоды
не теряются. Конфликтующее значение не выбирается для заголовка или признаков.

Заголовок — русский тип события и известные объекты, без генерации LLM.
Признаки — сортированные уникальные метки из структурированных полей и явных
таблиц в `bundle.py`; неизвестные отрасли/факторы не угадываются. Это подсказки
поиску, а не влияние на компанию. Topics по умолчанию — `other`.

Withdrawn и пустые claims дают `insufficient`. Активное событие получает partial
при неполноте B4, неизвестных существенных полях, недостающих признаках
идентичности B5, пропущенных текущих входах или неопределённых сопоставлениях.
Причины добавляются в limitations. Классификация использует метаданные и решения,
а не поиск фраз в тексте предупреждений. Конфликт, неизвестная дата публикации
или синтетическое происхождение сами по себе не понижают качество.

Withdrawn остаётся доступным по исходному ID с пустыми текущими свидетельствами;
заменившие ID перечислены в limitations. Отзыв не означает опровержения.
Смысловая корректность и истинность источников автоматически не установлены.

## Офлайн-проверки и совместимость

```bash
uv run --no-project --with-requirements llm/events/requirements.lock python -m unittest discover -s llm/events/tests -v
uv run --no-project --with-requirements llm/events/requirements.lock python -m llm.events.schemas --check
uv run --no-project --with-requirements llm/events/requirements.lock python -m llm.events.catalog_cli schema --check
uv run --no-project --with-requirements llm/events/requirements.lock python -m llm.events.check_contracts
uv run --frozen python -m unittest discover -s ranking/tests -v
```

Вместо `uv run --no-project --with-requirements ... python` можно использовать
`.venv/bin/python` с установленным lock. `test_bundle.py` использует временные
базы и существующий scripted offline provider: реальных запросов нет.
Установка зависимостей — отдельная операция, для неё может требоваться сеть.

**Ranking пока принимает v0.1.** Его код и контракт не менялись; B6 v0.2 он явно
отклоняет. Этот producer нельзя подключить к ranking без отдельной миграции
потребителя. Удаление scenario/value_role и неявный downgrade не реализованы.

Проверка границы с ranking выполняется, если в checkout присутствует
`ranking/b6.py`; иначе пропускается только этот интеграционный тест.
