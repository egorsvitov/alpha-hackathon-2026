# Интеграция и работа оркестратора

Это план будущих действий. Сейчас разрешено подготовить документы, но не реализацию и не запуск агентов. Три исполнителя: gpt-6.1-sol, reasoning medium; при фактическом запуске передавать отдельный self-contained prompt и repository path. Если среда требует fork=none для model override, использовать его и явно передать docs paths.

## I0 / E0 — обязательный контрактный этап

Оркестратор до параллельной работы:

1. Проверяет актуальные status/diff/ветку, существующие чужие changes. Не делает reset/clean, force push или merge main как часть этой задачи. Если нужны isolated worktrees, создавать от reviewable baseline с разрешёнными изменениями, не терять untracked документы.
2. Реализует `retriever/__init__.py`, нужные `channels/__init__.py`, `tests/__init__.py`, `evaluation/__init__.py`, `evaluation/embeddings/__init__.py` (последний потом может передать C), `representation_contracts.py`, `representation_ports.py` и contract tests. До реализации E0 сами эти файлы отсутствуют — не требовать агентов импортировать несуществующее.
3. Замораживает E0 API из contracts.md и примеры сериализации. Общий `normalize_embedding_text` (NFC + whitespace) фиксируется здесь же; A/B не создают разные нормализаторы. Python Protocols не импортируют реализации A/B/C; цикл `company_build -> company_views -> company_build` запрещён. Зависимости по типам в одном общем слое.
4. Реализует/планирует завершение R1: `retriever/contracts.py` и validator ImpactCandidateBatch v1. Уточняет scoped neighbor refs в payment_context, не разрешая произвольную чужую компанию. CandidateSet как новый параллельный формат не создаёт.
5. Добавляет retriever в package discovery. Согласует exact pins NumPy/ORT/tokenizers по проверенному окружению/uv.lock; optional GPU dependencies отделены, не конфликтуют с обязательным CPU runtime. Изменения root pyproject, uv.lock, .gitignore, THIRD_PARTY_NOTICES выполняет только оркестратор; не копировать автоматически все пакеты парсера из experiment.
6. Создаёт command dispatch `retriever/__main__.py`, держит ownership до финала; B предоставляет argparse registration. Все команды ниже будущие, пока E0/I3 не завершены они не существуют.
7. Передаёт A/B/C `contracts_frozen` revision и synthetic contract fixtures. До этого можно читать/планировать, нельзя писать собственные несовместимые types.

Конфигурации, которые E0 фиксирует (default или required):

| Тип | Поля |
|---|---|
| ViewPolicy | version=`company-views.v1`, template_config_path/hash, normalization=`nfc-whitespace.v1` |
| GraphPolicy | enabled=False; directions=(incoming,outgoing); weight_policy=sqrt_share; recipe_version=`neighbor-business.v1`; near_zero_norm=1e-12; hub_penalty=none |
| BuildConfig | output_root required, scope_ids/all required, source manifests required, ViewPolicy, GraphPolicy, EncoderConfig, company_chunk_size=128, temporal_mode=strict; published status only complete |
| QueryPolicy | enabled base3, graph flags False, graph recipe version/weight if enabled; temporal_mode=strict; view_order business/finance/trade_geo/incoming/outgoing |
| VariantConfig | variant_id, dense views, hybrid flag, frozen encoder/index/corpus/profile snapshot IDs, split/role/as_of/window, top_k_per_view=50, union_cap=250, pool_cap=100, output_limit=20, token_budget=8192, counter_version required, fusion k=60, channel weights=(1,1,1), reserve_policy=none for first controlled experiment |

Резерв unique-channel candidates в общем architecture.md — гипотеза, в первом контролируемом эксперименте выключен для всех. Это не перенос старого topic cap.

## I1 — три параллельные работы

- A1 base views; A2 persistent graph.
- B CPU/cache/resume на E0 fixtures; затем graph aggregator, optional GPU последним.
- C metrics/query/dense tests на E0 ports; labels form без подделки human judgments.

Оркестратор тем временем завершает согласованные ранее R3 и non-dense R4/R5: `event_views.py`, `eligibility.py`, `index.py`, `channels/lexical.py`, `channels/structured.py`, `fusion.py`, `service.py`. Это **реальная незавершённая зависимость**, не скрытая работа C. E5 backend берёт у B, dense channel у C, profiles у A. Если эти этапы не входят в ближайший разрешённый execution scope, выпускается company-embedding subsystem с явным `retrieval_integration_pending`; hybrid quality не заявляется.

## I2 — локальная интеграция без массового build

Порядок после review каждого исполнителя:

1. A1 + B: 3–8 synthetic profiles → views → objects → published snapshot → reopen/new process → те же refs/vectors. Cache unchanged повтор не вызывает модель.
2. A2 + B: небольшой платёжный fixture → правильные веса/coverage → incoming/outgoing; graph disabled base path работает без payments file.
3. B + C: tiny normalized event matrix → queries → exact hits; fingerprint mismatch/нет компании/omitted views обработаны явно.
4. Общие regression: `ranking.tests.test_profiles`, `ranking.tests.test_graph`, B6/contract/EventStore tests; затем relevant llm tests если затронут R1/R7. Не объявлять проход old fixtures готовой live analytics.
5. Validate file ownership по diff. Если A менял B/C-файл — вернуть изменение владельцу до merge. Изменения contracts — отдельное согласованное обновление всех потребителей.

## I3 — первый реальный путь к кандидатам

После R3–R5: до 8 выбранных компаний, до 100 fixed событий из existing corpus, CPU, без платных LLM. Encoder скачан/подготовлен явно; исходные B6 неизменны. Если нужны опубликованные дата/receipt, сохранить supplied values, не изобретать задним числом для обхода validation.

Проверить:

- B6 v0.2 → EventStore latest/as_of → eligibility/chunks → passage index;
- company cached query vectors → C dense + BM25/structured → R5 fixed fusion/budget;
- ImpactCandidateBatch содержит исходные snapshots/claims/conflicts/evidence, ссылки resolved;
- graph facts scoped, известный сосед не становится собственной отраслью клиента;
- `other`/empty taxonomy не запрещают dense/BM25;
- time/availability/withdrawn/insufficient проверки выполняются до отбора;
- все encoder fingerprints совпадают; old experiment matrices не использованы;
- negative/degraded/index failed не возвращаются как успешная нормальная лента.

## I4 — пилот и массовая подготовка

Только после разрешения execution и успешного I3; текущая planning-сессия не разрешает этот шаг.

1. Pilot <=256 target companies из разных отраслей и coverage; отдельно count dependency-only business neighbors. 512-token overflow count, unique text ratio, peak RSS, stages timings, холодный и тёплый cache.
2. На pilot инъекция crash/resume и edit одного business/product поля в копии fixture, не в source CSV. Проверить инвалидацию/удаление и перенос snapshot с referenced objects.
3. Проверить budget диска/памяти по pilot, согласованные model artifacts/license notices. Не обещать сроки по benchmark 100 news.
4. Full companies build: один repository load; один persistent payment ingestion на источник; base pass → graph pass если эксперимент включён → verify → publish. Не держать N×N, не вызывать attach_payment_graph 85k раз.
5. Full counts сравнить только если source hashes совпадают с audited. Report не содержит сырые data dumps в Git. CPU fallback/GPU configured variant — разные snapshots и compatible event indexes.

## I5 — оценка и аналитик

C выполняет evaluation.md на frozen dev/test. Если graph не улучшает результат/нет разметки, default остаётся base3. Нельзя менять gate после просмотра test.

R7 — отдельная задача оркестратора/владельца аналитика: новый batch context builder без legacy score, Bank RAG, source registry/QA и backend adapter. Сначала fixture E2E `ImpactCandidateBatch -> analyst -> API`, затем live LLM только после настроенных provider/model/price/budget (500 ₽/дневной лимит). Работающий embedding cache не требует такого вызова.

## Финальный review оркестратором

Проверить точность условий шаблонов; invalid/duplicate pair policy; стабильность semantic versions; scoped provenance; fingerprint/prefix/normalization; отключаемость графа; known vs vector coverage; суммы Decimal; memory bounds; snapshot portability; crash/resume; чёткие empty/error; no test leakage; no fake human labels/legacy score. Проверить, что actors не добавили обязательную vector DB/LLM/GNN.

Выдать отдельные статусы: company_embeddings_ready, payment_context_ready, dense_ready, hybrid_ready, quality_evaluated, analyst_integrated, gpu_verified. `false/not_evaluated` допустимы и не скрываются общим «всё готово».
