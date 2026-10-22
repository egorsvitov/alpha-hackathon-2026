# ImpactCandidateBatch v1 — проект контракта

Статус: спецификация для этапа R1. JSON Schema и runtime validator ещё не реализованы. Новый контракт не заменяет B6 v0.2 и не совместим автоматически со старым Relevance/feed.

## Два представления результата

`RetrievalResult = {candidate_batch, trace}`. `candidate_batch` содержит проверяемые данные для аналитика; `trace` — технические оценки, порядок каналов и причины сокращения. Аналитик не видит cosine/BM25/RRF, но получает trace_id для восстановления процесса.

### Верхний уровень candidate_batch

| Поле | Тип / смысл |
|---|---|
| schema_version | literal `impact_candidate_batch.v1` |
| request | request_id, company_id, role, as_of, window_start, window_end, requested_limit, max_input_tokens |
| status | ready / empty / degraded; ошибочный или устаревший индекс — явная ошибка API |
| empty_reason | null либо no_events / no_matches / budget_exhausted |
| company_context | profile_version, profile_hash, snapshot metadata, verified_facts, derived_exposures, group_context, payment_context, unknowns |
| candidates | массив Candidate, может быть пустым |
| retrieval_context | trace_id, index_snapshot_id, encoder_fingerprint, representation_version, policy_version, data_watermark, failed_channels |
| limitations | ограничения данных и покрытия |

Role: ceo/cfo/treasurer/chief_accountant. Datetime: seconds + timezone; as_of не смешивать с датой действия события. requested_limit — максимум, не требование заполнить ответ.

`verified_facts` означает подтверждённое происхождение из датасета, не независимый аудит фактической деятельности. Каждый элемент: feature_id, path, value, quality, origin. Derived exposure: exposure_id, factor, basis_feature_ids, rule_or_model_version, status=inferred, assumptions. Уровень confidence, если используется, — качественная оценка с описанной процедурой, не калиброванная вероятность.

### Candidate

| Поле | Смысл |
|---|---|
| candidate_id | стабильный ID по членам группы и версии grouping policy |
| event_cluster_id | null для одного события; при grouping — ID группы |
| cluster_version | null либо hash состава/версий/правил |
| members | список `{snapshot, bundle}`; MVP — один элемент |
| match_context | channels, matched_views, feature_ids, claim_refs, event_paths, hypotheses, unknowns |
| limitations | конкретные ограничения этой связи |

`snapshot`: event_id, event_version **integer**, payload_sha256, received_at, source_mode. `bundle` — исходный B6 v0.2 без изменения claims, evidence, sources, conflicts, relations, сценариев и measurement roles. Для старого v0.1 нужен явный отдельный compatibility path; MVP нового ретривера целится в v0.2, не маскирует версию.

Ссылки на утверждения составные: `{event_id, event_version, claim_id}`. Локальный claim_id не обязан быть уникален во всём батче. Аналогично evidence/source refs. Полная упаковка хранится в артефакте; LLM projection может убрать неиспользуемый технический JSON, но сохраняет все утверждения и конфликтные варианты выбранных событий, их evidence и ключевые временные/модальные поля. Если событие не помещается — отдельный запрос или явный overflow, не молчаливое обрезание доказательств.

`hypotheses`: text, status=inferred, basis_feature_ids, basis_claim_refs, derivation_version, unverified_assumptions. Пустой массив нормален. Dense-only match может содержать только совпавший раздел и retrieval evidence; не требовать выдуманный possible_mechanism для каждого результата.

`group_context`: идентификаторы и происхождение членства, наблюдаемые факты конкретных членов, missing_context. `payment_context`: not_loaded / unavailable / available; выбранные рёбра/агрегаты, direction, период, coverage, observed_share, provenance. Пустой список не означает отсутствие связей в реальном мире.

### Trace

Для каждого события: все каналы и ranks, исходные scores, winning view/chunk refs, нормализованный текст запросов/hash, RRF contributions, selected/omitted и reason. Trace также содержит версии алгоритмов, timing, размеры корпуса, кандидатов до/после budget, оценку токенов, truncation/chunking diagnostics. Сырые векторы сохраняются в индексе, не в каждом trace.

## Семантические проверки сверх JSON Schema

1. profile_hash и snapshot payload_sha256 соответствуют сохранённым объектам; ID и версии совпадают.
2. Все refs разрешаются, включая конфликтные варианты; ссылки на чужую компанию/несуществующий claim запрещены.
3. Ни данные, ни индекс не пришли из будущего относительно as_of; latest-версия выбрана до active-фильтра.
4. withdrawn/insufficient/невалидные bundles не попадают в selected candidates. partial не равен invalid.
5. Empty и degraded различаются. Если все каналы отказали — ошибка, не успешный пустой ответ. Если отказал один, разрешён явно настроенный degraded с журналом; для сравнения качества degraded-запуск помечается отдельно.
6. Порядок и результат воспроизводимы при одинаковых snapshot, profile, policy и encoder.
7. facts не пополняются выведенными свойствами. Для hypothesis ссылки доказывают предпосылки, а не истинность самого вывода.
8. Контракт работает для любого валидного профиля; нет ветвления по demo IDs.
9. Значения technical retrieval score не являются business significance и не передаются в старый UI score.

## Стык с аналитиком и бэкендом

Текущий `llm/agent/context.py` ожидает Relevance 0.1 и использует выбранные feature_ids. Потребуется новый context builder, принимающий batch/member + company_context, с сохранением проверок источников, времени, hashes и режима. Старый формат нельзя эмулировать выдуманными score/contribution.

Bank RAG — отдельный ответ с найденными чанками, document/chunk IDs, датами, страницами, точными цитатами и версиями. not_found — допустим, позиция банка тогда не генерируется.

Backend сначала вызывает retrieve_candidates, затем аналитика либо отдаёт честный статус pending_analysis. Готовая карточка/лента строится только из аналитического результата. Старый `backend/app/services/ranking_adapter.py` адаптирует другой формат и требует замены: проверить отказ B6 v0.2 и fallback, допускающий несовпадение hash.

Ключи кэша retrieval: profile hash, role, as_of/window policy, index snapshot, representation/fusion/grouping/budget versions. Кэш аналитики дополнительно включает полные member hashes, bank evidence/corpus revision, model/provider/prompt/QA versions и TTL для меняющегося контекста.
