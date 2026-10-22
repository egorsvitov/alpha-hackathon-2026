# Общие интерфейсы для заданий 01–07

Это проект интерфейсов, который должен реализовать агент задания 01. Точные JSON Schema будут результатом 01. До появления схем этот документ устраняет неоднозначности между исполнителями. Несовместимое изменение после 01 требует обновить зависимые схемы, fixtures и задания/README; не создавать вторую почти одинаковую структуру.

## Версии и базовые правила

| Объект | Версия | Владелец реализации |
|---|---|---|
| Внешний EventBundle | Существующая `0.1`, сохраняется без изменения | NLP; потребитель валидирует в 01 |
| CompanyProfile | Новая `0.1` | Схема 01, данные 02 |
| Relevance, RankingResult | Новые `0.1` | Схема 01, данные 03 |
| EvidenceBundle | Новая `0.1` | Схема и данные 04 |
| Insight | Новая `0.2`, отдельный файл | Схема/миграция 05, интеграция 06 |
| Исходный Insight v0.1 и его пример | Сохранить для совместимости и сравнения | Не переписывать молча |

Роли: `ceo`, `cfo`, `treasurer`, `chief_accountant`. Все машинные timestamps timezone-aware; для сравнения UTC, для дневного бюджета Europe/Moscow. В CLI без явного `as_of` применять текущие часы только если команда live; воспроизводимые demo-команды требуют явно заданного среза.

JSON должен быть строгим: отклонять NaN/Infinity и повторяющиеся ключи; ID непустые и непрозрачные. Не конкатенировать пользовательский ID в путь файла. Денежные/извлечённые величины хранить десятичными строками и считать через Decimal. Score и нормализованные доли допускают конечный JSON number с явно заданной точностью. Канонический хэш: UTF-8 JSON, sort_keys=True, separators без пробелов, allow_nan=False; порядок массивов входного B6 сохраняется.

Для ошибок использовать стабильный код, JSON Pointer/контекст и безопасное сообщение. Исключения programmer error не маскировать под `no_data`. Ошибка входа/профиля — явный failure boundary; ограниченный анализ корректного входа — доменный статус результата.

## CompanyProfile

Верхний уровень:

| Поле | Тип/правило |
|---|---|
| `schema_version`, `company_id`, `profile_version` | `0.1`, строка, строка |
| `is_synthetic` | boolean; true только для вымышленного профиля |
| `display_name` | Для реального обезличенного ID: «Компания <ID>» |
| `okved` | `{code: string|null, name: string|null}` |
| `industry` | Нормализованный код либо null; derived, не официальный полный классификатор |
| `products` | Все ключи обязательны: `rko`, `guarantee`, `loan`, `leasing`, `deposit`, `payroll`, `factoring`; boolean|null |
| `region`, `city`, `age_band`, `employees_band`, `development_stage`, `group_id`, `client_type` | string|null, исходные категории сохраняются |
| `countries`, `fts_present` | Массив нормализованных стран; boolean|null; неизвестность/конфликт сохраняется в features/limitations |
| `revenue` | `{raw: decimal-string|null, unit: string|null, unit_status: confirmed|unconfirmed, period: string|null}` |
| `graph` | Статус, версия, агрегаты; ниже |
| `features` | Словарь `feature_id → Feature`, не массив |
| `missing_fields`, `limitations` | Массивы строк; поля как JSON Pointers, ограничения как понятные сообщения/коды |
| `normalization_version` | Версия словарей/правил |
| `source_snapshot` | Метаданные выгрузки и временной применимости; ниже |

Feature: `{feature_id, path, value, quality, origin}`. `path` — JSON Pointer внутри профиля. Quality: observed/derived/synthetic/unknown/conflicting. Origin содержит тип источника, относительный путь/opaque ID, SHA-256, CSV data_record_number с 1 и колонку(и), либо ссылки на исходные features и версию производного правила. Полный raw-record в LLM не передавать.

Для 01A зафиксировать Origin как tagged union без произвольного dict: `csv={kind:"csv",source_path,source_sha256,data_record_numbers:[positive integer,...],columns:[string,...]}`; `derived={kind:"derived",input_feature_ids:[...],rule_version,artifact_id:string|null,artifact_sha256:string|null}`; `synthetic={kind:"synthetic",fixture_id,fixture_sha256}`; `manifest={kind:"manifest",source_path,source_sha256,field_path}` для подтверждённых метаданных выгрузки. Для производного графового признака artifact_id/hash обязательны и разрешаются в локальный артефакт с источником, записями и знаменателем. Unknown CSV-признак сохраняет csv origin, quality=unknown. Origin не может ссылаться сам на себя через input_feature_ids; все feature refs разрешаются в pinned profile.

`source_snapshot={source_sha256, imported_at, exported_at, data_as_of, historical_applicability}`. Первые четыре поля — SHA-256 и timezone-aware timestamps|null (imported_at обязателен и не null); historical_applicability=`unknown|dated_snapshot`. exported_at/data_as_of без manifest равны null; дата выгрузки не доказывает дату состояния. dated_snapshot допускается только при подтверждённом data_as_of и означает снимок на эту дату, а не интервал действия. Источник подтверждённой даты хранится в origin признака `/source_snapshot/data_as_of`. imported_at не входит в profile_version; содержательные даты и source hash входят. В историческом результате limitation отражает несовпадение as_of с датой снимка либо неизвестность даты, без автоматического утверждения исторической достоверности.

ID признака стабилен для `(company_id,path)`, например `U_3263:/products/loan`; конкретное значение идентифицируется вместе с profile_version. Version — хэш содержимого профиля и всех версий входов/нормализации, исключая source_snapshot.imported_at, время запуска и само поле версии. Модель признака одинакова для реального и синтетического профиля. Роль пользователя не входит в профиль.

Graph: `{status: not_loaded|loaded|unavailable, graph_version: string|null, period: null|string, currency: null|string, neighbors: [...], profile_coverage_by_nodes: number|null, graph_coverage: object|null, limitations: [...]}`. Neighbor: `{company_id, direction: incoming|outgoing, observed_amount_raw: decimal-string, payment_count: integer, observed_share: number 0..1, profile_known: boolean, feature_id}`. Пустой loaded-граф отличается от not_loaded. Версия графа учитывает источник и правила фильтрации. Состояние/значения графа входят в profile_version.

`graph_coverage` при loaded содержит оба ключа incoming/outgoing. Каждый — `{input_records, retained_records, excluded_ambiguous_records, excluded_invalid_records, retained_record_fraction, retained_amount_raw, valid_positive_input_amount_raw, retained_positive_amount_fraction, share_quality}`. Счётчики — nonnegative integers, суммы — Decimal-strings, fractions — number 0..1|null. input_records считает все строки направления для выбранного ID, кроме self-loops (они учитываются отдельно в артефакте); группы исключений не пересекаются, ambiguous имеет приоритет, input_records=retained_records+excluded_ambiguous_records+excluded_invalid_records. retained_record_fraction=retained_records/input_records, при 0 — null. retained_amount_raw — знаменатель долей оставшегося поднабора. valid_positive_input_amount_raw — сумма всех положительных finite сумм до исключений, **включая повторяющиеся строки**; retained_positive_amount_fraction=retained_amount_raw/эта сумма только если суммы всех input_records валидны и знаменатель >0, иначе null. Это техническая доля записанного объёма, не оценка реальных операций при дублях.

share_quality=`clean_observed|limited|unavailable`: clean_observed только если retained_records>0 и нет ambiguous/invalid потерь; limited если retained_records>0 и есть потери; unavailable если retained_records=0. Неподдающаяся определению направленность/ID отражается глобальным issue и консервативно ограничивает качество, не исчезает из отчёта. Для not_loaded/unavailable graph_coverage=null. Даже clean_observed не подтверждает период, валюту или экономическую зависимость. Сильный share-based бонус допускается только для clean_observed; limited допускает максимум слабый сигнал наличия сохранённой связи без использования процента, см. 03.

UI-адаптер переименовывает guarantee/loan/deposit в guarantees/loans/deposits. Внутренний формат не дублирует оба написания.

Файл с несколькими профилями имеет форму `{"schema_version":"0.1","profiles":[CompanyProfile,...]}`; company_id уникальны. CLI `profile` возвращает один CompanyProfile, CLI с `--profiles` читает этот envelope. Для него 01 создаёт отдельную `company-profiles.schema.json`, не смешивая формат с одиночным профилем.

## Снимки событий и время

Хранилище держит bundle без добавления полей внутрь него, отдельно — `payload_sha256`, `received_at`, `source_mode=live|fixture`, `ingest_id`. Стабильный ключ `(event_id,event_version)`. Поддерживаются импорт JSON и JSONL, повторная загрузка и получение снимка на срез.

event_version — положительный JSON integer из B6, сравнение численное (10 > 9), не строковое. SnapshotRef — `{event_id,event_version,payload_sha256,received_at,source_mode,ingest_id}`; StoredEvent содержит те же метаданные и bundle. `ingest_id` стабилен для сохранённого snapshot и не меняется при unchanged. Validation warnings хранятся в ingestion metadata вне B6 и доступны в отчёте.

`received_at` назначает принимающая система, а не текст новости. `updated_at` producer тоже не должен быть позже исторического среза. Для fixture replay использовать отдельные тестовые часы и изолированную БД, явно помечая replay; не переносить эту возможность бесконтрольно в live API.

Общий ingestion: `ingest_bundles(bundles, *, store, clock, source_mode) -> list[IngestResult]`. Он валидирует весь batch и записывает его одной транзакцией с общим received_at=clock.now(). Clock выдаёт timezone-aware время. `EventStore.put` — низкоуровневый доверенный интерфейс принимающей стороны; у клиента новости нет полномочий назначать received_at. CLI ingest, rank fixture-import и demo вызывают ingest_bundles, затем `list_latest(as_of=...)`; нельзя иметь второй decoder/receipt policy внутри rank. rank принимает либо `--state-dir`, либо `--events` для fixture-import, не оба источника сразу. `--events` требует `--mode fixture --fixture-clock`, создаёт временную БД, если fixture state-dir не выбран отдельной согласованной опцией. Live rank не импортирует JSONL и не принимает fixture-clock. Для нескольких моментов получения replay выполняет последовательные batch-ingest с соответствующими fixture clocks.

На `as_of` сначала выбрать максимальную версию из **известных на этот момент** снимков, затем применить availability/quality. Нельзя сначала удалить withdrawn и выбрать прежнюю active-версию — это воскресит отозванное событие. Поздно пришедшая старая версия не откатывает новую. Новая active-версия с большим номером может восстановить событие; промежуточный отзыв остаётся в истории.

Дата публикации не должна быть из будущего для обычной выдачи. Будущий effective_at/forecast_horizon допустим. Неизвестная дата публикации допускает хранение и ограниченную карточку по прямому запросу, но в MVP исключается из автоматической ленты с кодом `publication_date_unknown`. Историчность профиля из недатированного CSV отдельно не доказана: воспроизводим конкретный снимок профиля и не обещаем состояние компании на любую дату прошлого.

## Relevance и RankingResult

Relevance обязательные поля: schema_version, event_id, event_version, company_id, profile_version, user_role, as_of, eligibility, reason_codes, score, raw_total, scoring_diagnostics, level, reasons, candidate_channels, feature_ids, claim_ids, evidence_status, limitations, scoring_version, rules_sha256.

- `eligibility`: eligible/deferred/excluded. Причину исключения/отсрочки хранить в `reason_codes`.
- `score`: number 0..1 или null. `level`: low/medium/high/unknown. При null только unknown. Отсутствие правила не означает доказанный нулевой эффект; такая пара deferred/unknown.
- `evidence_status`: usable/limited/insufficient. Это пригодность оснований, не вероятность истинности и не quality producer.
- Reason: `{reason_id, group, text, raw_points, contribution, feature_ids, claim_ids, event_paths, basis}`. Basis: observed_match/conditional_mechanism/metadata_match/lexical_match. Event paths относятся к сохранённому bundle.
- Бизнес-причина должна иметь feature IDs; конкретная смысловая связь — claim IDs. Причины на одних метках помечаются metadata_match, без выдуманного claim. Поправки свежести могут иметь пустой feature_ids.
- Для scored-пары вклады неотрицательны и в сумме дают score (допуск 1e-6). Базовая формула в 03; raw_points сохраняются для диагностики.
- `evidence_status=limited` не обязан снижать business priority: спорная важная новость может быть приоритетной для проверки.
- raw_total — конечное неотрицательное number (сумма weighted_g до /100 и saturation), null для unscored. scoring_diagnostics — null для unscored либо `{groups:[{group,raw_points,role_multiplier,age_weight,weighted_points}],age_policy:decay|upcoming_floor|durable_recheck_floor,score_saturated:boolean}`. Все численные поля конечны и неотрицательны; raw_points ограничен cap **до** role multiplier. Одна запись на группу, лучшая причина выбирается детерминированно. raw_total=sum(weighted_points), score=min(1,raw_total/100), score_saturated=(raw_total>=100). Вклады reasons распределяются из тех же групп и дают score. Насыщенные результаты различаются по raw_total; это эвристика, не вероятность.

RankingResult: schema_version, company_id, profile_version, user_role, as_of, input_snapshot_hash, scoring_version, rules_sha256, `ranked` (все scored-кандидаты), `feed` (после порога/разнообразия), `deferred`, `excluded`, limitations. Точный wrapper ranked/feed — `{snapshot: SnapshotRef, relevance: Relevance}`; deferred/excluded — `{snapshot: SnapshotRef, reason_codes:[string,...], limitations:[string,...]}`. Полный bundle доступен через store, не теряется при сокращении ответа. Feed — подмножество ranked без перерасчёта relevance. Неразмеченные исключения нельзя считать неважными автоматически.

Дополнительный бизнес-контекст (планы найма, цели, watchlist) пока не является входом v0.1: не добавлять придуманные флаги в CSV-профиль или текстовый prompt. После рабочего MVP его можно ввести отдельным версионируемым CompanyContext с происхождением, подтверждением пользователя и сроком применимости; он должен войти в ключи кэша и traces. Без этого интерфейса ранкер оценивает потенциальную значимость по доступному профилю, а не доказывает личный недельный top-3 руководителя.

## EvidenceBundle

Поля: schema_version, status=`ok|partial|no_bank_evidence|unavailable`, event_id/version, company_id/profile_version, as_of, corpus_version, query_plan, hits, limitations.

`query_plan`: попытки с запросом, topic-фильтрами, temporal mode и причиной. `hits[]`: evidence_id, document_id, document_sha256, chunk_id, page, char_start/end, quote, text_sha256, source_path, title, published_at/date_status, verification_status, retrieval_score, matched_queries, limitations, is_synthetic. Score поиска не является confidence. Quote точно соответствует выбранному диапазону, все хэши и координаты проверяемы. verification_status: pending_review/visually_checked_excerpt/synthetic. Для synthetic corpus is_synthetic=true, даже если фрагмент имитирует банковский обзор.

Для MVP hit может совпадать с целым chunk (диапазон и quote уже определены). Если выделяется более короткая цитата, evidence_id обязан учитывать её границы. Одному hit нельзя после хэширования молча заменить текст.

Корпусная версия учитывает содержимое, метаданные, exclusions и версию поиска, а не только список PDF. У каждого найденного фрагмента сохраняется pending_review, пока именно этот фрагмент не проверен; проверка страницы заголовка не проверяет все числа документа.

Одна retrieve_evidence попытка, attempt=0/1/2, при >0 нужен retry_reason. query_plan — список записей попыток, фактически выполненных данным вызовом (одна запись); оркестратор хранит историю между вызовами. Entry содержит attempt, queries, topic_filters, temporal_mode, retry_reason, config_version. Optional diagnostics хранит причины rejection/manual-review candidates; не добавлять непригодные источники в hits ради полноты. `insufficient_relevance` — limitation/reason при no_bank_evidence, не новый status.

## Insight v0.2 и реестр оснований

В файле `llm/agent/contracts/insight.v0.2.schema.json` сохранить смысл v0.1 и явно добавить:

- event_version, profile_version, `generation_mode=live|fixture`, `generated_at`;
- `relevance` вместо разрозненного ручного significance: целиком согласованная Relevance;
- `section_states` для what_happened/why_important/risk/opportunity/next_step/bank_view: `{status: available|not_identified|insufficient_evidence|not_applicable, reason: string|null}`;
- `unknowns[]`: стабильный id, текст, affected_paths, clarification_question|null;
- `claims[]`: поля прежней claim плюс `input_claim_ids[]`, `modality` и `polarity`. Для source_fact modality/polarity берутся из B6 и не null; для bank_view — собственная модальность/полярность банковского сообщения (unknown, если не установлена). Для ai_inference оба поля null: это отдельный вывод с условностью через support_status/assumptions, а не ещё один извлечённый B6 claim. input_claim_ids может быть пустым для bank_view; для source_fact непустой, для ai_inference перечисляет реально использованные новостные claims;
- `sources[]`: news/bank_analytics/synthetic_news, locator, quote, дата, publisher/url/content_kind, is_synthetic и координаты/версии происхождения; для банковского — doc hash/chunk/page. Искусственный банковский фрагмент может иметь type=bank_analytics только с is_synthetic=true, verification_status=synthetic и только в явном demo; нельзя выдать его за материал банка в live;
- features_used — массив выбранных Feature; все feature IDs проверяются по pinned profile;
- trace: artifacts, шаги, qa_status, retries, corpus_version, config/prompt/model IDs; usage с actual/estimated/unknown/no_llm_calls;
- статус ready/limited/needs_clarification/insufficient_evidence/budget_exhausted/failed/provider_unavailable. Отзыв события обрабатывается сервисом как недоступная актуальная карточка, а не старый ready-ответ.

Новая схема не добавляется поверх v0.1 без смены schema_version. Валидный минимальный пример v0.2 и `docs/insight-v0.2-migration.md` создаются в 05 до генератора, без утверждения, что агент уже работал. EvidenceBundle schema/fixture создаются в 04 до retriever; core-схемы из 01 не меняются молча.

Реестр sources строит код до генерации. Для news source_id детерминированно генерировать из `(event_id,event_version,evidence_id)`, например `news:` + полный sha256 канонического JSON-кортежа; для bank — `bank:` + sha256(document_sha256,chunk_id,char_start,char_end,text_sha256). В source хранить исходный evidence_id и article ID/version. Это изменение относительно предложенного в B6 адаптера к Insight v0.1 нужно явно описать в миграции v0.2. B6 сам не меняется.

LLM выбирает только существующие source IDs и feature IDs. Названия, URL, цитаты, хэши и значения профиля код берёт из реестра, а не из ответа модели. В итоговом ответе могут быть только фактически использованные sources/features. Значение confidence моделью не запрашивать как замену доказательству.

## Публичные Python-интерфейсы

Возвращаемые JSON-объекты проверяются схемами. Ниже сигнатуры-цели; допускаются dataclasses внутри, но внешний shape должен совпадать.

```python
# 01: ranking/b6.py, ranking/event_store.py
validate_bundle(bundle: dict) -> list[ValidationIssue]
load_bundles(path: Path) -> list[dict]  # JSON object или JSONL по расширению
ingest_bundles(bundles: list[dict], *, store: EventStore, clock: Clock,
               source_mode: str) -> list[IngestResult]  # атомарный batch
EventStore.put(bundle: dict, *, received_at: datetime, source_mode: str) -> IngestResult
EventStore.get_latest(event_id: str, *, as_of: datetime) -> StoredEvent
EventStore.list_latest(*, as_of: datetime) -> list[StoredEvent]

# 02: ranking/profiles.py, ranking/graph.py
CompanyRepository(csv_path: Path, *, normalization_config: Path)
CompanyRepository.get(company_id: str) -> dict  # CompanyProfile
attach_payment_graph(profiles: list[dict], known_company_ids: set[str],
                     payments_path: Path, *, config: dict) -> tuple[list[dict], dict]

# 03: ranking/engine.py; snapshots уже содержат received_at, не просто bundle
rank_events(snapshots: list[StoredEvent], profile: dict, *, role: str,
            as_of: datetime, rules: dict, entity_mapping=None,
            top_k: int = 5) -> dict  # RankingResult

# 04: llm/rag/retriever.py
retrieve_evidence(bundle: dict, profile: dict, relevance: dict, *,
                  as_of: datetime, corpus: Corpus, config: dict,
                  attempt: int = 0, retry_reason: str | None = None) -> dict

# 05: llm/agent/orchestrator.py
analyze_event(snapshot: StoredEvent, profile: dict, relevance: dict, *,
              role: str, as_of: datetime, services: Services) -> dict  # Insight v0.2
answer_question(run_id: str, question: str, *, company_id: str,
                role: str, services: Services) -> dict  # внутренняя QaResult

# 06: llm/agent/service.py — вызывается backend, здесь нет HTTP
PersonalizationService.get_feed(company_id: str, *, role: str, as_of: datetime) -> list[dict]
PersonalizationService.get_insight(company_id: str, event_id: str, *, role: str, as_of: datetime) -> dict
PersonalizationService.ask(run_id: str, question: str, *, company_id: str, role: str) -> dict
PersonalizationService.feedback(run_id: str, *, company_id: str, useful: bool) -> None
```

Типы `ValidationIssue`, `IngestResult`, `StoredEvent`, `SnapshotRef` и Protocol Clock объявить в `ranking/contracts.py` в 01 и импортировать в следующих заданиях. `ValidationIssue={code,path,message,severity:error|warning}`; `IngestResult={status:inserted|unchanged,snapshot:SnapshotRef,issues:[ValidationIssue,...]}`. StoredEvent описан выше; validation issues доступны вне payload. Ошибки store: event_not_found, event_version_collision; decoder отдельно различает неверный JSON и нарушение контракта.

Services вводится в 05: event_store, profile_repository, retriever, generator, qa, run_store, budget, config, clock. Generator — Protocol `generate(request: GenerationRequest) -> GenerationResult`, с моделью/usage и строгим JSON payload; budget.reserve обязателен для любой live-реализации. Контракты request/result и бюджетных операций описать до добавления реального клиента, затем переиспользовать в 06.

QAReport имеет status/next_action/issues/checks из 05 и независимые structural_status, semantic_status, publication_status. Точные dataclasses определяет единственный владелец ports.py в 05A. В отсутствие независимого смыслового review semantic_status=not_run, допустим только limited результат после структурных/критичных guards; fixture не становится ready. QA/history/retriever переиспользуют этот порт, не создавая конкурирующие типы.

## CLI и артефакты

Будущие команды: `python -m ranking validate-b6`, `ingest`, `profile`, `rank`, `demo`; `python -m llm.rag retrieve`; `python -m llm.agent analyze`, `ask`; `python -m evaluation.run` и `python -m evaluation.demo`. В заданиях указаны аргументы. Ошибки/диагностика — stderr, JSON-результат — stdout или явно выбранный output. Не смешивать лог с JSONL.

Состояние по умолчанию: `data/processed/personalization/`, с отдельными events.sqlite, runs.sqlite, budget.sqlite и artifacts/. Тесты используют tempfile. Сервис принимает state_dir; read-only код в Docker не должен требовать записи рядом с исходниками. Общий budget.sqlite подходит только процессам на одном локальном томе; для разных хостов нужен общий ledger service, иначе гарантия командного потолка недостижима.

Из `RankingResult.ranked` получать Relevance конкретного event ID/version с удалением только технического wrapper; не создавать её вручную по названию компании. CLI rank дополнительно поддерживает `--relevance-event-id <ID> --relevance-output <path>` для RAG/agent smoke; отсутствующая scored-пара возвращает явный статус, а не придуманный score. Contract-fixture Relevance из 01 можно использовать только в явно помеченном isolated тесте.
