# Передача адаптера retriever → analyst

Репозиторий: `egorsvitov/alpha-hackathon-2026`.
Ветка: `feature/analyst-retrieval-adapter`.
Реализация: коммит `661c0c4`; база аналитика: `ef84f53`.
Фактическая исходная ревизия комплекта и SHA-256 каждого файла находятся в
`MANIFEST.json` внутри ZIP. Последующий коммит добавляет сам архив.

## Быстрый запуск из архива

Распакуйте ZIP в отдельную папку. В ней сохранена структура каталогов репозитория.
Для локального примера и тестов нового адаптера все исходные зависимости включены.
Рекомендуется Python 3.13; исходная проверка также проходила на Python 3.14.7/Linux.

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
.venv/bin/python docs/handoff/analyst-retrieval-adapter/run_example.py
```

Ожидаемый результат: `status=ready`, три кандидата, все состояния `ready`,
`matches_reference=true`, `llm_calls=0`. Скрипт проверяет совпадение с приложенными
AnalystInput/diagnostics. Ключ API для него не нужен. Для сохранения результата:

```bash
.venv/bin/python docs/handoff/analyst-retrieval-adapter/run_example.py --output-dir output
cd backend
../.venv/bin/python -m unittest tests.analyst.test_retrieval_adapter -q
```

Ожидается 35 тестов, один пропуск для необязательных приватных batch.json.
Этот комплект включает FakeLLM и необходимые публичные синтетические fixtures.
Полный backend/API и весь регрессионный набор запускаются из checkout ветки.
Для Docker/Linux Python 3.13 есть дополненный `backend/requirements.lock`.

## Работа из репозитория

```bash
git fetch origin
git switch --track origin/feature/analyst-retrieval-adapter
```

Если локальная ветка уже существует, переключитесь на неё обычным `git switch`.
Архив также содержит `changes.patch`: это изменения от `ef84f53` до ревизии из
manifest. Для переноса на отдельную чистую ветку от этой базы:

```bash
git apply --check /path/to/unpacked/changes.patch
git apply /path/to/unpacked/changes.patch
```

Patch включает реализацию и handoff-инструкции; сам ZIP в patch не включён.
Подкаталоги с поддерживающим кодом в архиве взяты из той же исходной ревизии.

## Интерфейс

```python
from app.analyst.retrieval_adapter import adapt_retrieval_batch

adapted = adapt_retrieval_batch(
    batch, company_profile, active_interests, "cfo",
    {"max_input_tokens": 8000, "max_projection_chars": 24000},
)
analyst_input = adapted.analyst_input
diagnostics = adapted.diagnostics()
```

`batch` — `retrieval-candidate-batch.v1`; профиль и активные интересы передаются
отдельными словарями. Все три объекта должны относиться к одной компании,
версии профиля и версии интересов. Поля, форматы и исключения описаны в
`docs/reviews/retriever-analyst-integration-20261024.md`.

Интеграция с уже созданным сервисом:

```python
output, adapted = service.analyze_batch(
    batch, "cfo", company_profile=company_profile,
    active_interests=active_interests,
    analysis_policy={"max_input_tokens": 8000},
)
```

Создавайте `AnalystService` с принятыми backend settings. `ANALYST_MODE=cached`
использует сохранённые результаты; для новых событий возможен `analysis_failed`
при отсутствии кэша. Для изолированного бизнес-анализа в тестах используется
FakeLLM. Реальные LLM-вызовы зависят от настроек сервиса и отдельного бюджета.
Проверка и преобразование адаптером выполняются локально без LLM.
Старый `impact_candidate_batch.v1` маршрутизируется через прежний адаптер.

## Что читать и передавать дальше

- `docs/reviews/retriever-analyst-integration-20261024.md`: таблица трёх контрактов,
  правила валидации, результаты тестов, открытые вопросы и план подключения к ленте.
- `docs/examples/retriever-analyst/`: полный синтетический batch, профиль, интересы,
  преобразованный AnalystInput и diagnostics. Два события одной reading group
  после split сохраняются раздельно.
- `backend/app/analyst/retrieval_adapter.py`: основная реализация.
- `backend/app/analyst/service.py`, `view.py`, `qa.py`, `schemas.py`, `analyst.py`:
  маршрутизация, доказательства в промпте, QA, диагностика, кэш и бюджет.
- `backend/app/analyst/contracts/`: зафиксированный JSON Schema retriever.
- `backend/tests/analyst/test_retrieval_adapter.py`: raw/merge/split, повторы,
  даты, версии, ссылки, состояния, переполнение, кэш и старый формат.
- `MANIFEST.json`, `changes.patch`: ревизии, состав, контрольные суммы и патч.
- `build_archive.py` рядом с этим README: воспроизводимая сборка ZIP из Git
  по явному перечню. Требуется checkout репозитория.

## Существенные границы

`reading_group_ids` описывает группу публикаций; `canonical_event_id` определяет
событие. Raw article identity не доказывает тождество двух публикаций об одном
событии. При повторном merge с новым event ID сквозная семантическая дедупликация
пока требует отдельного контракта.

Только точные оригинальные цитаты становятся claims/evidence. Пересказ merge
передаётся в `derived_context` и помечен производным. Проверка refs и срезов
не доказывает истинность источника или смысловую корректность пересказа.
Cosine и векторы не становятся бизнес-оценкой или фактами компании.
`partial/deferred/unresolved/invalid/context_too_large` требуют явного учёта;
они не означают бизнес-нерелевантность. Неизвестная дата остаётся неизвестной.

Вход аналитика подключён; для ленты ещё нужны job/оркестратор, хранение результатов,
mapper в FeedItem/Insight и API/UI для точности дат и состояний обработки.

В комплекте только код, контракты, документация и публичная синтетика.
Локальные новости, приватные профили, рабочие БД, API logs и ключи не включены.
Первоначальные проверки: 469 тестов аналитика (4 пропуска), 35 тестов адаптера с
локальными данными, 53 ranking, оба E2E template/cached. Подготовка комплекта
дополнительно проверяет запуск примера и нового набора из распакованного ZIP.
