# B6: публикация и экспорт EventBundle v0.2

B6 явно публикует согласованный снимок B5. Он сохраняет утверждения, сценарии,
роли значений, конфликты и точные цитаты без изменения семантики. Чтение не
запускает B3–B5. Внешние схемы и примеры v0.1/v0.2 не менялись.

## Запуск

Python 3.13+, зависимости из `llm/events/requirements.lock`. Для существующей
SQLite-базы версии 1–3 выполнить явную миграцию; каждый шаг создаёт SQLite backup.
Новая база сразу использует схему 4. База и производные файлы остаются локальными.

```bash
python -m llm.events publish --db data/processed/events/state.sqlite3 migrate
python -m llm.events publish --catalog news plan
python -m llm.events publish --catalog news publish
python -m llm.events publish --catalog news get EVENT_ID
python -m llm.events publish --catalog news export
```

До этого статьи проходят [B2](storage.md), [B3](extraction.md), [B4](normalization.md),
а их итоговые артефакты явно выбираются в [B5](catalog.md).
`--db` и `--catalog` задаются перед подкомандой. CLI не создаёт базу при опечатке.

## API и история

```python
from llm.events import EventStore, EventBundleService, PublicationConfig

service = EventBundleService(EventStore(), catalog_id="news")
plan = service.plan_publication()             # без записи
receipt = service.publish_snapshot()          # publish() также поддерживается
bundle = service.get_event(event_id)          # JSON-совместимый dict v0.2
old = service.get_event(event_id, event_version=1)
snapshot = service.export_events(publication_revision=1)  # список bundles
old_in_snapshot = service.get_event(event_id, publication_revision=1)
changes = service.changes_since(1)            # изменившиеся bundles, включая withdrawn
basis = service.get_provenance(event_id, event_version=1)
export = service.export_snapshot()            # каталог events.jsonl + manifest.json
```

`event_version` и `publication_revision` в `get_event` взаимно исключаются.
Историческое чтение не требует актуальности B5 и не пересчитывает старые правила.
`event_id` общий для B5/B6, но публичный `event_version` B6 независим от версии B5.
Ревизия публикации тоже отличается от ревизии каталога. Первая публичная версия — 1;
изменение содержимого, включая метки и ограничения, повышает её. Изменения только
внутреннего аудита могут создать новую публикацию без повышения версии события.
Точный повтор сохраняет объект, `updated_at` и возвращает `cached`.

```bash
python -m llm.events publish --catalog news get EVENT_ID --event-version 1
python -m llm.events publish --catalog news get EVENT_ID --publication-revision 1
python -m llm.events publish --catalog news changes --since 1
python -m llm.events publish --catalog news provenance EVENT_ID --event-version 1
```

Текущие чтения отклоняют `catalog_stale`, `bundle_not_published` и `b6_stale`;
последние два — сохранённые имена ошибок первой реализации B6. Неизвестные ID и
публикации дают `event_not_found` и `publication_not_found`. CLI возвращает
`issues: [{code, path, message}]`, код выхода 1; ошибки аргументов — код 2.

## Правила, качество и происхождение

[`publication_vocabulary.json`](../publication/publication_vocabulary.json) — строгий словарь
точных условий. Критерии категории, объекта, действия и показателя соединяются
через AND; значения внутри критерия — альтернативы. Все условия полей относятся
к одному утверждению. Каждая метка имеет трассу: правило, claim ID, путь поля,
реальные evidence ID либо явное классификационное основание без цитаты.
Тема `other` используется при отсутствии более конкретного правила. Отрасль
не выводится из названия компании. `counterparty` не назначается автоматически,
а пошлина не считается санкцией без соответствующего предмета меры.

Заголовок использует русскую категорию и известную общую метку объекта/показателя.
Прогноз, план, предложение и оценка получают свой префикс; смешанная/неизвестная
модальность — «Сведения». Числовой результат и победитель конфликта не выбираются.

`insufficient` — отзыв либо отсутствие структурированного действия, объекта и
измерений у всех утверждений. `partial` — неполнота B4, диагностика, неопределённость
сопоставления, неизвестная модальность/полярность или существенное время/стадия.
Для прогноза нужен горизонт, наблюдаемого показателя — период, решения/действия —
временная привязка. Неизвестное необязательное действие или объект сами по себе
не понижают качество. Конфликт не мешает `complete`. Этот статус не означает
истинность публикации, полноту извлечения или независимую смысловую проверку.

В происхождении сохраняются правила и хэши, B4-артефакты, версия/ревизия B5,
решения, замещения, трассы признаков и структурированные причины качества.
Метаданные записываются неизменяемо вместе с публикацией в существующий
`rules_json`; дополнительной миграции после v4 нет. У старых публикаций первой
реализации трассы отсутствуют: `get_provenance` возвращает `provenance_unavailable`.
Их история остаётся доступной; новая явная публикация не переписывает прошлое.

Правила закрепляются за каталогом. Для обновления словаря по умолчанию:

```bash
python -m llm.events publish --catalog news publish --rebuild
python -m llm.events publish --catalog news plan --config config.json
python -m llm.events publish --catalog news publish --config config.json
```

Конфигурация соответствует `PublicationConfig`: `rules_version`, `max_events`
(по умолчанию 10 000, включая отозванные), необязательный `dictionary`.
Изменение конфигурации применяется только явно. Превышение лимита не обрезает
снимок. Публикация проверяет происхождение B4, архивы, все bundles и grounding,
затем повторно проверяет ревизию/поколение B5 и конкурентную публикацию перед
одной транзакцией. При гонке — `catalog_changed`, без частичного снимка.

## Экспорт

По умолчанию `export_snapshot()` и CLI создают новый каталог под
`data/processed/events/exports/` (рядом с выбранной БД). `--output DIRECTORY`
задаёт другой путь. Внутри:

- `events.jsonl`: полный EventBundle v0.2 на строку, сортировка по ID;
- `manifest.json`: ревизии B6/B5, run ID, хэши конфигурации и схемы, число записей,
  SHA-256 точных байтов JSONL и `complete_snapshot`.

Отозванные события включены по умолчанию. `--active-only` устанавливает
`complete_snapshot=false`: такая выгрузка не подходит для полной синхронизации.
Пустой опубликованный каталог даёт пустой JSONL и корректный manifest.

Файлы готовятся и синхронизируются во временном каталоге, затем краткая
`BEGIN IMMEDIATE` блокирует изменения и повторно проверяет актуальность перед
атомарным переименованием. Исторический экспорт задаётся через
`--publication-revision`; актуальность текущего каталога ему не требуется.
Готовые каталоги неизменяемы: существующий путь даёт `export_exists`.
При ошибке временные файлы удаляются, прежняя выгрузка сохраняется.

Для совместимости явный путь с расширением `.jsonl` у `export_events(path)`
и CLI сохраняет старый одиночный файл **без manifest**, с той же защитой от гонок.
Для передачи потребителю используйте каталог. `export_events()` без пути
возвращает список bundles; `export_snapshot()` — файловую выгрузку.

Минимальный пример потребителя:

```bash
python -m llm.events.examples.read_bundles data/processed/events/exports/SNAPSHOT/events.jsonl
```

Он проверяет v0.2, исключает `withdrawn/insufficient`, сохраняет конфликты и
использует `(event_id, event_version)` как ключ кэша. Ранжировщик не реализуется.

## Проверки

```bash
python -m llm.events publish schema --check
python -m llm.events.contracts.schemas --check
python -m llm.events catalog schema --check
python -m llm.events.contracts.check
python -m unittest discover -s llm/events/tests -v
```

Все проверки используют временные БД и офлайн-провайдер. Интеграция с ranking
пропускается, если его модуль отсутствует в checkout; миграция потребителя v0.1
на v0.2 остаётся отдельной задачей. Обязательные проверки не обращаются к API.
