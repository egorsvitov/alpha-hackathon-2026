# Задание агенту 06: QA, общий бюджет, кэш и интеграция

Доведи модуль персонализации до управляемого запуска и передачи результата backend. Прочитай [COMMON.md](COMMON.md), [INTERFACES.md](INTERFACES.md), код и отчёты 01–05. Тарифы/ключи/модели не угадывать. Если live-конфигурации нет, выполнить все offline/concurrency/adapter проверки и честно отметить live not_run.

## Два подэтапа

**06A — инфраструктура расходов.** Можно начинать после 01 и согласования ports из задания 05. Создать общий BudgetGateway, provider boundary, tests. До готовности шлюза 05 работает только fixture.

**06B — QA, кэш и сервис.** После 05 подключить шлюз, усилить QA, реализовать актуальность/инвалидацию, UI-adapter и передать backend готовые Python-вызовы/пример подключения.

В обоих случаях это одно задание с едиными интерфейсами; не создавать два конкурирующих budget/QA модуля.

## Файлы и границы

- `llm/agent/budget.py`, `provider.py`, `config.py`;
- доработка `qa.py`, `run_store.py`, `orchestrator.py` из 05;
- `llm/agent/cache.py`, `ui_adapter.py`, `service.py`;
- `llm/agent/tests/test_budget.py`, `test_provider.py`, `test_qa.py`, `test_cache.py`, `test_ui_adapter.py`, `test_service.py`;
- `.env.example`: только имена переменных/безопасные значения, default live disabled;
- `docs/personalization-backend-integration.md`: точная инструкция backend-разработчику и список необходимых изменений UI;
- синтетические output fixtures для API, согласованные с типами;
- `docs/implementation/06-result.md`.

Пользователь отвёл backend/frontend другим участникам. В этом задании делать готовый сервисный адаптер, примеры, контрактные проверки и конкретный integration diff/document. Не переделывать их UI или ORM самостоятельно без отдельного поручения. Если они уже интегрировали сервис — проверить их код и устранить ошибки в своей границе. Недостающие изменения типов UI должны быть перечислены с конкретными файлами/полями, а не замаскированы данными.

## 06A. Конфигурация и provider

Сначала проверить `llm/agent/model_policy.json`. Все текущие routes disabled, provider_model_id=null. Display name не является provider ID. Использовать только подтверждённое соответствие разрешённой модели реальному ID; local fixture модель не выдаётся за участника allowlist.

Предлагаемые конфиги:

```text
LLM_ENABLED=false
LLM_API_BASE_URL=
LLM_API_KEY=
LLM_PROVIDER_MODEL_ID=
LLM_REQUEST_TIMEOUT_SECONDS=...
LLM_MAX_OUTPUT_TOKENS=...
LLM_DAILY_TOKEN_LIMIT=...
LLM_TEAM_DAILY_RUB_LIMIT=500
LLM_MODULE_DAILY_RUB_QUOTA=...
LLM_INPUT_PRICE_RUB_PER_MILLION=...
LLM_OUTPUT_PRICE_RUB_PER_MILLION=...
PERSONALIZATION_STATE_DIR=...
LLM_BUDGET_DB_PATH=...
```

Численные пропуски — блокирующая live-ошибка config_incomplete. Не подставлять чужие тарифы или предполагать, что подписка ChatGPT оплачивает API. Backend/NLP должны использовать тот же gateway или согласованную систему квот; если часть вызовов идёт мимо, отчёт обязан сказать, что глобальный расход не гарантируется.

HTTP-клиент должен быть тонким, с test transport, таймаутами, пределом ответа и структурированными ошибками. API-формат подтвердить по конфигурации/документации фактического провайдера, не предполагать OpenAI compatibility по одному названию модели. Secret headers/prompt contents не писать в обычные логи. Runtime dependency добавить в lock/notices.

Provider не возвращает готовый success при transport error, обрезанном JSON или finish_reason=length. Ошибка JSON не чинится бесконтрольным повторным запросом. Каждый дополнительный платный вызов имеет отдельный attempt и budget reservation.

## Бюджетный ledger

Для MVP на одном сервере допустим общий SQLite с transactions, WAL/busy_timeout и ограниченными retries на lock. Для разных хостов один SQLite-файл «у каждого свой» не обеспечивает командный лимит; подготовить API-port для общего сервиса либо держать live disabled до общего ledger/квот.

Хранить день Europe/Moscow, module, run/request/attempt IDs, модель и price version, зарезервированные верхние границы токенов/стоимости, фактическое usage, состояние и timestamps. День не задаёт клиент.

Состояния: reserved → dispatched → settled или uncertain; reserved → cancelled_unsent. Переходы идемпотентны. Каждый вызов reserve в транзакции проверяет командный и модульный остаток с учётом active reservations и неопределённого расхода.

Оценка максимальной стоимости:

```text
upper_cost_rub =
  input_tokens_upper_bound * input_price_rub_per_million / 1_000_000
  + max_output_tokens * output_price_rub_per_million / 1_000_000
```

Деньги Decimal либо целые micro-rub с округлением reservation вверх. Верхняя граница input должна учитывать сообщения/обёртку/инструменты и специфику провайдера. Оценка «символы / 4» не годится как гарантия. Использовать подтверждённый tokenizer/provider count или явно безопасный верхний предел для данного запроса; если верхняя граница неизвестна — fail closed. Не обещать ограничение расходов на основании средней длины.

Резервировать input+output токены по определению дневного лимита провайдера. Cached input discount не применять к верхней границе, пока тариф/usage однозначно не подтверждены. После ответа settle по actual usage, если все тарифные компоненты известны. Иначе хранить conservative reservation и status unknown/estimated, не объявлять actual.

При timeout после отправки расход мог состояться: `uncertain`, не полный возврат денег. При отмене до dispatch можно освободить reservation. Истёкшая запись после падения процесса не становится автоматически бесплатной; нужен reconcile с провайдером или консервативный учёт.

Reset в 00:00 МСК создаёт новый day bucket; старые unsettled записи сохраняются. Перед dispatch после долгой очереди перепроверить дату/резервирование. Для вызова, пересёкшего полночь, согласовать billing-day semantics провайдера; до подтверждения применять консервативную политику и не освобождать неоднозначный расход. Тестировать через injected clock, не системное время.

Гарантия потолка зависит от всех вызовов через ledger и корректных тарифов/верхних границ. Эти условия должны быть проверяемы конфигурацией и описаны в отчёте.

## 06B. Финальный QA

Усилить существующий QA из 05:

1. Схема Insight v0.2, identity, references, source metadata, profile values и contribution sums.
2. Точная цитата соответствует registry и исходному диапазону; модель не меняла числа/URL/source types.
3. Source reports сохраняют модальность/отрицание/стадию, не смешивают claims разных событий.
4. Персональные claims ссылаются на реально использованные признаки и сохраняют unknown/conflicting limitations.
5. Bank_view поддержан bank evidence с подходящим предметом/датой; свежая банковская публикация по другой теме не является подтверждением.
6. Проверить единицы, range/qualifier, % против п.п., observation period против horizon. Если вычисления есть — сверить отдельный calculation artifact.
7. Независимо проверить, не использовано ли contested field как согласованная основа; scope conflict распространяется на производные вычисления.
8. Источники не имеют будущих дат относительно режима, текущий event не withdrawn, profile/event versions не устарели во время долгой генерации.
9. Section states соответствуют содержимому; empty недостаточных данных не превращается в «рисков не выявлено».

Для семантической проверки использовать короткий независимый review prompt по claims/цитатам, если разрешённая модель и бюджет доступны; его стоимость входит в общий gateway. Reviewer выдаёт структурированные verdicts, не доверие в процентах. Автоматический reviewer не гарантирует истины; при отсутствии проверки — explicit not_run/limited, а не ready по одной JSON Schema.

OCR pending_review не переводить в visually_checked_excerpt по ответу LLM. Критичные числа/графики требуют подтверждённого текста/ручной проверки; иначе соответствующие bank claims ограничить или не публиковать. Безопасные качественные выводы отдельно оценивать, не закрывать автоматически весь ответ из-за одного непроверенного числа.

## Кэш и параллельные запросы

Ключ: company ID/profile version, role, event ID/version/payload hash, `as_of` и temporal mode, rules/mappings, corpus version, prompt/config/model, QA version. Формировать детерминированно. Не включать secrets.

- Cache hit только если все зависимости совпадают и текущая policy допускает результат.
- Live-запрос проверяет latest availability/version до отдачи cached и после длительной генерации. Если появился отзыв/новая версия, старый ready не публикуется как актуальный.
- Исторический replay использует свой pinned as_of и явно отличается от актуальной карточки.
- Не кэшировать временный provider error как вечный ответ; TTL/negative caching явно настроены.
- Два одинаковых конкурентных запроса не должны оба платить за один analysis: single-flight/lease. Потерянная lease не позволяет списать один attempt дважды и не делает dispatched call бесплатным.
- Кэш не отменяет backend authorization. company_id/role обязательно входят в проверки; исключить межклиентскую утечку по одному event_id.
- `generated_by=cached` ставить по реальному попаданию, source mode fixture/live сохранять отдельно.

## UI-адаптер и договорённости

Прочитать `frontend/src/types.ts`, `components/Claims.tsx`, `components/EventBlocks.tsx`, `api/index.ts` и backend schemas. Сейчас backend заглушки расходятся с типами; готовность Python JSON ещё не означает готовность работающего экрана.

Сделать функции преобразования профиля, ленты, Insight и QaResult. NewsEvent.sources_count — число distinct article_id текущих sources, не число цитат/claims и не число независимых свидетелей. is_synthetic=true, если использован синтетический новостной компонент, с явным описанием смешанного происхождения. `has_bank_position` только при реально существующих проверенных bank claims, не по одному topic match или результату retrieval.

Feed не должен запускать LLM для всех элементов ради has_bank_position: использовать уже актуальный валидный Insight, если он есть; иначе положительная пометка пока отсутствует. Утверждать «у банка точно нет позиции» по такому false нельзя. Все банковские synthetic fixtures отдельно маркируются и не входят в реальные live-ответы.

Изменения, требующие согласования с владельцем UI:

| Сейчас | Требуется |
|---|---|
| CompanyProfile.is_synthetic литерал true | boolean для реальных обезличенных профилей |
| revenue_bn_rub обязательное число | nullable и honest label при неизвестных единицах; не выдавать raw/1e9 как подтверждённые рубли |
| NewsEvent.date string | nullable/неизвестная дата с корректным рендерингом |
| Relevance level без unknown | Явная неопределённость либо safe adapter с limitation, не тихая замена на «малозначимо» |
| status только ok/insufficient_data | Представление limited, unavailable/budget и их причин |
| Empty ClaimList показывает «Не выявлено» | Использовать section_states, различать отсутствие результата и отсутствие выявленного риска |
| Insight UI не содержит generation_mode | Добавить fixture/live отдельно от generated_by=cached/live; кэшированный тестовый ответ остаётся тестовым |

До согласования этих изменений unsupported cases возвращать явный adapter error/insufficient result по согласованному минимуму, не фальсифицировать поля. Подготовить точный список TypeScript полей и примеры, чтобы backend/frontend разработчик мог применить изменения. Существующие synthetic API examples сохранить или версионировать.

В `Claims.tsx` feature label ищется по `cf:${key}`. Адаптер должен последовательно передавать keys и соответствующие evidence IDs для признаков, не ломая provenance на произвольных коротких названиях. Для news/bank evidence использовать registry IDs и поддерживать обратную связь к B6/PDF.

## Сервис для backend

PersonalizationService из INTERFACES.md не должен зависеть от FastAPI globals. Constructor получает repositories, state/config/clock. Backend вызывает его после проверки доступа. Подготовить пример dependency injection и преобразования доменных ошибок:

- unknown company/event → 404;
- invalid role/input → 400/422;
- withdrawn → согласованный статус недоступности (например 410), не старая карточка;
- version conflict on ingest → 409;
- provider/budget → доменный status либо договорённый HTTP error, с UI пояснением;
- ask: run/company/role проверены, usage учитывается;
- feedback сохраняется append-only с run ID и оценкой, не вызывает автоматическую перенастройку весов.

Новостной upload остаётся ответственностью NLP/backend. Наш сервис принимает его EventBundle. Если producer ещё не готов, E2E использует явный fixture import; нельзя выдать моку за работающий парсер.

Отдельно описать Docker-интеграцию: текущий backend build context `./backend` не включает ranking/llm/data. Backend-владельцу потребуется корректный root context/package, зависимости ML и read-only mounts входных данных, writable state_dir в volume. Ключи не включать в image. Не утверждать, что существующий compose уже запускает новые модули.

## Проверки

B01–B08, Q01–Q07 и I01–I08 из матрицы. В частности:

- Две конкурентные резервации на последний остаток: проходит только допустимая; повтор settle идемпотентен.
- Token cap срабатывает даже при остатке рублей и наоборот.
- Crash после dispatch/timeout не освобождает расход как нулевой.
- Полночь/очередь/перезапуск проверены injected clock и реальным SQLite, не только fake object.
- Provider transport fake: 429/500/timeout/invalid JSON/truncation; каждый billable retry учтён.
- Cache между компаниями/ролями/версиями не смешивается; отзыв во время generation блокирует публикацию.
- UI fixtures: unknown revenue/date, partial, conflict, forecast, budget_exhausted, cached/live, fixture/live, отсутствие bank_view.
- Все source/feature links UI разрешаются в источники; frontend typecheck запускается только если действительно менялись/подключались его типы.

```bash
uv run --frozen python -m unittest discover -s llm/agent/tests -v
# Если frontend-владелец уже применил согласованный контракт:
npm --prefix frontend run typecheck
```

Live smoke — только при полном конфиге и выделенной квоте: один маленький synthetic пример, actual usage и provider ID в локальном отчёте без ключа. Если выполнить нельзя, все независимые части довести до готовности, live пометить not_run с конкретными отсутствующими параметрами.
