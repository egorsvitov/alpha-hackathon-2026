# Обновление news-pipeline: готовность B6

Подтянуто в `news-personalization`: `2453da0` → `fd79650`, 7 коммитов, 95 файлов. Локальная реализация персонализации сохранена; конфликты `.gitignore`, notices и README разрешены с сохранением обеих областей. Коммиты/push не выполнялись.

## Что реализовано upstream

| Блок | Фактическая функция |
|---|---|
| B1 | Pydantic-модели, схемы/примеры EventBundle v0.2, проверки ссылок, точных цитат и разметки |
| B2 | SQLite-архив версий статей, состояния заданий и батчей |
| B3 | Извлечение кандидатов через SDK-адаптер, строгий ответ, журнал расходов; live требует явных тарифов/лимитов/квоты |
| B4 | Проверка доказательств и нормализация чисел, дат, единиц и алиасов |
| B5 | Поиск сходных утверждений и каталог событий с версиями, stale/withdrawn, историей и ручными решениями; LLM-рекомендация не применяется автоматически |
| B6 | Схема/примеры есть; основной экспорт EventBundle из каталога ещё не реализован |

Основание: `llm/events/README.md` прямо оставляет экспорт B6 следующему этапу. `CatalogService.get_event` в `catalog_store.py` возвращает внутренний CanonicalEventRecord b5.1, который в `catalog_models.py` явно отделён от транспортного B6. Это ещё не выдача согласованного EventBundle с верхнеуровневыми event/ranking_features.

`get_event/export_events` найдены также в `llm/events/legacy/pipeline.py`, но это изолированный несовместимый ранний прототип, не основной producer. `legacy/README.md` предупреждает об этом и о необходимости отдельного адаптера из сборщика `news/` в основной ArticleInput.

## Изменения входного контракта

EventBundle теперь v0.2: SourceClaim содержит scenario, Measurement — value_role (`reported/before/after/comparison`). Это позволяет не смешивать сценарии прогноза и значения до/после. Добавлены семь synthetic примеров, включая cross-sentence, before-after и scenarios. Старые v0.1 schema/fixtures сохранены.

`compatibility.upgrade_v01` явно переводит 0.1→0.2, добавляя scenario=null/value_role=reported; он не восстанавливает утраченную семантику. Обратного неявного преобразования нет.

Наш `ranking.b6` пока принимает v0.1. На новых примерах v0.2 подтверждён отказ валидации. Перед подключением нужны завершённый B6 exporter и миграция потребителя с сохранением новых полей; удалять scenario/value_role ради старой схемы нельзя. Этот обзор миграцию не реализует.

## Проверки

- `uv run --no-project --with-requirements llm/events/requirements.lock python -m unittest discover -s llm/events/tests -q` — 177 тестов passed. Зависимости установлены из отдельного lock, наш pyproject/uv.lock не менялся.
- `uv run --frozen python -m unittest discover -s ranking/tests -q` — 53 теста passed.
- Проба `ranking.b6.validate_bundle`: normal v0.1 принят, normal v0.2 отклонён.
- `git diff --check` прошёл после разрешения конфликтов. Платные LLM и backend не запускались.

Итог: содержательная часть препроцессора продвинулась до каталога событий, но готового совместимого потока B6 для нашего ранкера пока нет. Следующий стык — exporter B5→B6 v0.2 и версия входа нашей персонализации.
