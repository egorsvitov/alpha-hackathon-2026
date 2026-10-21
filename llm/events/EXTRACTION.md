# B3: кандидаты утверждений и исполнитель заданий

B3 выполняет один запрос для одной неизменяемой редакции статьи. Результат — `ExtractionResult` версии `b3.1`, где `verification_status=unverified`. Статус задания `succeeded` означает структурную пригодность ответа для B4. B3 не создаёт `SourceClaim`, `CanonicalEvent` или `EventBundle`, не доказывает истинность, полноту или смысловую поддержку утверждений.

[B4 реализован](NORMALIZATION.md): команда `normalization_cli normalize --artifact-id <id>` преобразует итоговый артефакт B3 в проверенные по структуре/цитатам `SourceClaim`, сохраняя отказы, предупреждения и предположения датировки. Явный однократный repair использует общий механизм B3; автоматического исправления в извлечении по-прежнему нет.

## Установка и проверка без сети

Из корня репозитория, Python 3.13+:

```bash
.venv/bin/python -m pip install -r llm/events/requirements.lock
.venv/bin/python -m unittest discover -s llm/events/tests -v
.venv/bin/python -m llm.events.schemas --check
.venv/bin/python -m llm.events.extraction_cli schema --check
.venv/bin/python -m llm.events.check_contracts
```

SDK OpenAI 2.54.0 и httpx 0.28.1 закреплены вместе с транзитивными зависимостями. Обязательные тесты используют `httpx.MockTransport`, сценарный `OfflineProvider`, временные БД и управляемые часы. Ключ, внешние статьи и доступ к сети для тестов не нужны. SDK импортируется только при вызове транспорта.

## Быстрый офлайн-пример

Используйте отдельную демонстрационную БД. `fixtures/b6_articles.json` содержит полностью вымышленные `ArticleRecord`; в батч B2 передаются вложенные `article`:

```bash
.venv/bin/python -c 'import json; from pathlib import Path; p=Path("data/processed/events/demo-articles.json"); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps([json.loads(Path("llm/events/fixtures/b6_articles.json").read_text())[0]["article"]], ensure_ascii=False))'
.venv/bin/python -m llm.events.storage_cli --db data/processed/events/demo.sqlite3 import-batch data/processed/events/demo-articles.json
```

Из квитанции скопируйте `items[0].source_id` и выполните:

```bash
.venv/bin/python -m llm.events.extraction_cli prepare \
  --db data/processed/events/demo.sqlite3 \
  --profile llm/events/profiles/offline.json --source-id <source_id>
.venv/bin/python -m llm.events.extraction_cli enqueue \
  --db data/processed/events/demo.sqlite3 \
  --profile llm/events/profiles/offline.json --source-id <source_id>
.venv/bin/python -m llm.events.extraction_cli run \
  --db data/processed/events/demo.sqlite3 \
  --profile llm/events/profiles/offline.json \
  --limits llm/events/profiles/offline-limits.json \
  --offline-responses llm/events/profiles/offline-responses.json --max-jobs 1
```

Сценарный ответ сообщает ноль утверждений и содержит ограничение «no extraction performed». Это демонстрация API и хранения, а не извлечение из текста. Он имеет отдельные ключ кэша и бюджетное пространство `offline:YYYY-MM-DD`; реальные задания не могут переиспользовать его как результат модели. Каждая запись файла `--offline-responses` — объект `{body, status_code, headers}` для одного вызова; исчерпание списка не запускает настоящий провайдер.

## Контракт для B4

[JSON Schema](../../docs/contracts/extraction-response.b3.1.schema.json) генерируется командой `extraction_cli schema`. Модели — источник истины. [Синтетический пример](fixtures/extraction-response.b3.1.json) содержит разные базы сравнения, несколько предложений, сценарии и пересмотр прогноза.

`ExtractionResponse` содержит:

- `claims`: локальный `claim_id`, текст, один из 11 `event_type`, модальность, отрицание, стадия, автор, участники, действие/объект, география, сценарий, четыре времени, измерения и `field_evidence`;
- `evidence`: локальный `evidence_id`, `field=title/text`, точная цитата; координат пока нет;
- `excluded`: поле, исключённая цитата и причина;
- `limitations`: ограничения источника или извлечения.

Все обязательные ключи должны присутствовать; неизвестные допустимые значения передаются как `null`. Участники имеют `name`/`role`, автор — `name`. Канонических `entity_id` нет. `CandidatePeriod` содержит только `text`. Измерение сохраняет исходные `value_text`, `unit_text`, показатель, `kind`, квалификатор, роль значения и исходный период сравнения. Значения вроде `0,5`, `1,1 трлн` и диапазоны остаются строками; знак изменения B4 устанавливает из исходного контекста, а не из одного токена числа.

`parse_extraction(content)` проверяет один JSON-объект без markdown-ограждений, повторных ключей, нештатных констант, лишних полей и невалидного Unicode. Проверяются строгие типы, перечисления, уникальность ID утверждений/доказательств/измерений, существование ссылок и JSON Pointer. ID измерений уникальны во всём ответе, остальные — в своём пространстве. Ссылки полей должны входить в `evidence_ids` утверждения. Полнота покрытия и соответствие цитат статье здесь намеренно не проверяются.

`ExtractionResult` оборачивает ответ в `schema_version`, `source_id`, `article_sha256`, `processing_spec_hash`, `verification_status` и `extraction`. Эти поля назначает код. Локальные ID имеют смысл только внутри одного сохранённого результата. B4 получает `get_cached_result(source_id, spec).payload` и исходный `get_article(source_id)`, проверяет цитаты/координаты, нормализует числа, даты и организации, назначает ID следующего уровня. Отдельная успешная обработка B4 должна иметь собственный `ProcessingSpec`.

## Библиотечный API и кэш

```python
from llm.events import EventStore, ProviderProfile, ExecutionLimits
from llm.events import build_processing_spec, ExtractionRunner
from llm.events.provider import OfflineProvider, ProviderResponse

store = EventStore('data/processed/events/demo.sqlite3')
profile = ProviderProfile.model_validate_json(open('llm/events/profiles/offline.json').read())
limits = ExecutionLimits.model_validate_json(open('llm/events/profiles/offline-limits.json').read())
spec = build_processing_spec(profile)
# source_id берётся из квитанции B2. Импорт не ставит задания сам.
# store.enqueue(source_id, spec)
provider = OfflineProvider([ProviderResponse(body={
    'choices': [{'finish_reason': 'stop', 'message': {'content':
        '{"claims":[],"evidence":[],"excluded":[],"limitations":["Offline example"]}'}}],
    'usage': {'prompt_tokens': 0, 'completion_tokens': 0},
})])
runner = ExtractionRunner(store, provider)
outcomes = runner.run_pending(spec, limits, max_jobs=1)
```

`run_next(spec, limits)` получает максимум одно текущее задание; необязательный `source_id` ограничивает выбор статьёй. `run_pending` требует положительный конечный `max_jobs`. При пустой очереди возвращается `idle`. `enqueue` уже успешного задания возвращает прежнее задание, вызова не будет; кэш доступен через B2. `probe` явно сообщает `cached`, когда выбранная статья уже успешно обработана с той же спецификацией.

В ключ кэша входят режим offline/live, endpoint, модель, параметры генерации, reasoning, формат ответа, ограничения/таймауты профиля, версии и хэши промпта и схемы. Тарифы, денежная квота и ключ не входят. Изменение промпта или схемы требует новой спецификации; исполнитель отклоняет старую вместо запуска с другими настройками.

Реальный адаптер использует `OpenAIProvider(profile)`; SDK выполняет ровно один HTTP-вызов, `max_retries=0`, без streaming, tools и перенаправлений. Использовать его через исполнитель, который отвечает за проверку лимитов и резервирование. Низкоуровневый `complete` предназначен для транспорта, а не обхода бюджета. Контроль SDK тестируется на подменённом HTTP-транспорте.

## Реальные запросы: сейчас заблокированы

`profiles/provider.example.json` и `limits.example.json` заведомо не разрешают сеть. Для настройки скопируйте их в игнорируемый `data/processed/events/`, задайте подтверждённые значения и сначала выполните:

```bash
.venv/bin/python -m llm.events.extraction_cli validate-profile \
  --profile data/processed/events/provider.json --limits data/processed/events/limits.json
```

Требуются `live_enabled=true`, `tariffs_confirmed=true`, `limits_confirmed=true`, цены входа/выхода в рублях за миллион токенов, отдельная дневная квота NLP `0 < quota <= 500`, реальные верхние границы входа/выхода и общего контекста. `max_input_tokens + max_tokens` не должны превышать подтверждённый контекст. Границы берутся из подтверждения провайдера; B3 не угадывает их по названию модели. `max_request_bytes` — дополнительный согласованный предел полного сериализованного запроса со схемой и промптом, не оценка числа токенов. При превышении статья получает `input_too_large`, без обрезания и обращения к API. Ошибка контекстного лимита самого провайдера также не вызывает обрезание или повтор.

Начальный лимит ответа — 4096 токенов, connect timeout — 5 секунд, read timeout — 60 секунд. Начальный формат — `text` с требованием JSON в промпте. `json_object`/`json_schema` требуют `format_verified=true` для обычного запуска. `thinking` и `reasoning_effort` по умолчанию не передаются; заданные значения требуют `thinking_verified=true`. В этой версии разрешён ровно предоставленный ID `deepseek/deepseek-v4.1-flash`; смена модели требует явного расширения маршрута и проверки, не fallback.

Поддержка JSON Schema и отключения reasoning прокси пока не доказана. Документация DeepSeek описывает `extra_body={"thinking":{"type":"disabled"}}`: [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/). Этот параметр можно проверить отдельной командой, а не включать автоматически. SDK-интерфейс основан на [Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

```bash
.venv/bin/python -m llm.events.extraction_cli probe \
  --db data/processed/events/state.sqlite3 \
  --profile data/processed/events/provider.json --limits data/processed/events/limits.json \
  --source-id <source_id> --max-requests 1
```

`probe` допускает неподтверждённые флаги формата/reasoning для их проверки, но никогда не пропускает денежные ограничения. Можно явно передать до трёх `--source-id`, их количество ограничивает `--max-requests` (1–3). Одна статья — максимум один запрос, без автоматического повторного открытия `failed/deferred`. Повторный тест после ошибки требует явного `storage_cli retry-job`; изменение настроек создаёт другую спецификацию. Проба использует тот же структурный парсер кандидатов, сохраняет расход и ответ; HTTP 200 само по себе не подтверждает соблюдение схемы. Успешная проба не изменяет файлы профиля: результат просматривает разработчик.

Ключ берётся из `LLM_API_KEY`. Только CLI читает буквальное присваивание из локального `.env`, не заменяя переменную процесса; импорт библиотеки не читает `.env`. Ключ не передаётся в спецификации, JSON запроса или журнале заголовков. В ответе транспорта точное значение ключа редактируется; сохраняются только диагностические response headers (`Retry-After`, request ID, rate-limit). Сырые ответы и исходные статьи остаются локальными.

## Состояния, аренда и ошибки

Исполнитель сохраняет запрос и полный ответ до разбора результата. Ответ транспорта содержит HTTP status, безопасные headers, JSON body и исходный HTTP body в `raw_body` с редактированием ключа. Даже невалидный JSON провайдера остаётся текстом для аудита.

Попытка имеет аренду B2 на 15 минут, heartbeat продлевает её каждые 30 секунд отдельным потоком. Сетевой вызов не удерживает SQLite-транзакцию. Ошибка heartbeat запрещает успешное завершение. Потеря аренды/смена редакции оставляет ответ в журнале вызовов, но не публикует текущий результат. B2 по-прежнему атомарно сохраняет итоговый артефакт и `succeeded`.

| Ситуация | Результат |
|---|---|
| Корректный полный ответ, включая ноль утверждений | `succeeded`, кандидаты `unverified` |
| `finish_reason=length` | `failed/output_limit_exceeded`, даже при пригодном префиксе JSON |
| Пустой content, отказ, tools, неожиданный finish reason, нарушение JSON/схемы/ссылок | `failed`, отдельный код, без кэша |
| 429 | `deferred/provider_rate_limited`, `Retry-After` сохранён, запуск остановлен |
| 401/403, другие HTTP-ошибки (включая 400/5xx) | `failed`, запуск остановлен |
| Таймаут/ошибка соединения | `failed`, запуск остановлен, расход неопределённый |
| Недостаточный резерв/блокировка бюджета | `deferred`, вызова нет, запуск остановлен |
| Статья слишком велика | `deferred/input_too_large`, переход к следующему заданию |
| Новая редакция или истёкшая аренда | результат `superseded`, ответ/расход доступны для аудита |

Отдельная ошибка статьи не останавливает остальные, если нет общей проблемы провайдера или бюджета. Повторы выполняются только явно через B2. Ошибка процесса между резервом и сохранением ответа оставляет `reserved`; истечение аренды не освобождает деньги. После аварии сначала остановить прежний исполнитель, затем `storage_cli requeue-expired`, проверить журнал и при необходимости выполнить сверку.

## Денежный журнал

Перед вызовом резервируется `(max_input_tokens * input_price + max_tokens * output_price) / 1_000_000` через `Decimal`, в одной `BEGIN IMMEDIATE` транзакции с проверкой активной попытки. Для одного attempt возможен только один вызов. Верхняя граница целого входа берётся консервативно, не из приблизительного токенизатора. Скидки за кэш не уменьшают резерв и расчётный расход.

По `usage.prompt_tokens` и `usage.completion_tokens` рассчитывается сумма по сохранённым тарифам. Reasoning уже входит в completion и второй раз не добавляется. `usage.cost` остаётся непрозрачным полем ответа; его неизвестные единицы не конвертируются в рубли. Некорректный/отсутствующий usage и транспортный сбой сохраняют полный резерв как `unknown`. Успешный структурный результат при неизвестном расходе возможен, но деньги остаются зарезервированы.

Состояния вызова: `reserved`, `settled`, `unknown`, `reconciled`. Ответ, идентичность вызова и исходный резерв неизменяемы. Сверка меняет только учётную сумму/основание; поздний ответ после сверки можно прикрепить для аудита, не заменяя подтверждённую вручную сумму.

Сутки — `Europe/Moscow`. Квота фиксируется первым успешным резервированием в конкретном пространстве дня; другой процесс не может поднять её другим конфигом. Неопределённые резервы предыдущих дней вычитаются из доступной квоты нового дня. Сумма по usage или число токенов, превысившие зарезервированные пределы, включают блокировку последующих запросов, сохраняющуюся после полуночи. Снять её можно только явно с объяснением.

```bash
.venv/bin/python -m llm.events.extraction_cli calls --db <db>
.venv/bin/python -m llm.events.extraction_cli show-call --db <db> <call_id>
.venv/bin/python -m llm.events.extraction_cli budget --db <db>
.venv/bin/python -m llm.events.extraction_cli budget --db <db> --offline
.venv/bin/python -m llm.events.extraction_cli reconcile --db <db> <call_id> \
  --actual-rub <confirmed_amount> --reason "Сверено с учётом провайдера"
.venv/bin/python -m llm.events.extraction_cli acknowledge-overrun --db <db> \
  --day live:2026-10-21 --reason "Причина превышения проверена, параметры исправлены"
```

Сверка допускается только для `reserved/unknown` после закрытия активной попытки; сам `requeue_expired` не означает подтверждения нулевой стоимости. Команды не запрашивают баланс у провайдера. Локальный журнал ограничивает только NLP-процессы, использующие одну БД; расходы других участников с общим ключом он не видит. Для общей командной гарантии нужен общий шлюз. До появления реальных тарифов и NLP-квоты живой режим остаётся закрыт.

## Миграция B2

Новые базы получают `user_version=2`; application ID остаётся `0x414C5048`. К семи таблицам B2 добавлены `budget_days` и `llm_calls`. Старые версии статей, задания, попытки и артефакты не переписываются.

```bash
.venv/bin/python -m llm.events.extraction_cli migrate --db data/processed/events/state.sqlite3
```

Открытие базы v1 сообщает `storage_migration_required`; автоматической миграции нет. Команда блокирует писателей, создаёт согласованную резервную копию v1 через SQLite backup с учётом WAL, затем транзакционно добавляет таблицы и переводит версию в 2. При ошибке схема остаётся v1; резервная копия сохраняется. Путь копии возвращается в результате, его можно задать `--backup`; существующий файл не перезаписывается. Копия по умолчанию лежит рядом с исходной БД и остаётся в игнорируемом каталоге. Чужая, неполная или неизвестная схема отклоняется.

Все команды выводят JSON. Ошибки возвращают `issues=[{code,path,message}]` и код 1, неверные аргументы CLI — код 2. Запуск с failed/deferred/superseded результатами возвращает код 1; `idle` или успешный офлайн-пример — код 0.

После добавления B5 актуальная схема базы — 3. Старый шаг `1 → 2` сохранён; для полного перехода используйте `python -m llm.events.catalog_cli --db <path> migrate`. Подробности — [CATALOG.md](CATALOG.md).
