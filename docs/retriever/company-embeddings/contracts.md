# E0 — общие контракты и инварианты

Статус: спецификация будущего Python API, не реализованный модуль. Владелец `retriever/representation_contracts.py` и `retriever/representation_ports.py` — оркестратор. Агенты начинают общую реализацию после E0. JSON-поля snake_case, schema_version `company_representations.v1`. Объекты frozen dataclasses с `to_dict()`; массивы в JSON не включать. Перечисленные ниже поля обязательны, допускаемые null указаны явно. Все ошибки — типизированные ValueError-наследники с `code`, `message`, `context` без секретов.

## 1. Существующие форматы остаются

CompanyProfile остаётся из `ranking/profiles.py`, B6 v0.2 и StoredEvent — существующие. Их идентичность, hashes и временные проверки сохраняются. Новые representations — sidecar, а не поля с выдуманными значениями в профиле. Результат R5 остаётся `{candidate_batch, trace}` с `impact_candidate_batch.v1`.

Полные CompanyProfile и платежные CSV не передаются encoder. Encoder видит только текст. Feature provenance и контекст хранятся отдельно.

## 2. Типы компании

`BaseViewKind = business | finance | trade_geo`; `ViewKind` дополнительно universal, graph_incoming, graph_outgoing, graph_combined, graph_incoming_text, graph_outgoing_text. Последние три — только отключённые экспериментальные варианты.

`FeatureRef = {company_id: str, profile_version: str, feature_id: str}`. Разрешение всегда против конкретного сохранённого профиля. Нельзя разрешать по одному feature_id против текущей изменившейся таблицы.

`CompanyView`:

| Поле | Тип / правило |
|---|---|
| company_id, profile_version | str; идентичность профиля |
| kind | BaseViewKind либо universal/graph_*_text |
| text | str, непустой, без prefix; только подтверждённые факты и явно отделённые поисковые темы |
| text_hash | SHA256 UTF-8 нормализованного текста |
| content_hash | hash kind + template_version + отобранные значения/качества/unknowns; без company_id, profile_version, полного CSV hash, времени импорта и номеров строк |
| template_version | str, версия шаблонов и словарей |
| feature_refs | list[FeatureRef], отсортированный, без повторов |
| facts | list[{text, feature_refs}], дословно детерминированные утверждения об исходных полях |
| search_topics | list[{text, basis_feature_refs, status: conditional}], не устанавливают реальную экспозицию |
| quality | observed / derived / limited; conflicting поле не используется как факт |
| unknowns, limitations | list[str] |
| source_provenance | dict: полные profile/source hashes, source dates/quality; сохраняется даже при reuse vector |

`CompanyViews = {company_id, profile_version, views: dict[BaseViewKind, CompanyView], omitted: dict[BaseViewKind, reason]}`. Для каждой из трёх категорий ровно одно из views/omitted. Причины: no_supported_activity, no_positive_products, no_supported_trade_or_registered_region. Universal строится только для evaluation поверх доступных base views, не дублируется в default mapping.

Общая функция E0 `normalize_embedding_text(text: str) -> str` принадлежит оркестратору и используется A/B без независимых копий. Нормализация текста v1: Unicode NFC, strip, повторные пробельные символы → один пробел; регистр и смысловые знаки сохранить. Prefix добавляет только encoder. Не помещать технические IDs/hash, имя «Компания U_…», пропуски или выручку в embeddings. Верификация содержательных refs обязательна.

### Точные шаблоны v1

- business: `Основная деятельность: {okved_name}. ОКВЭД: {code}.` + `Отрасль: {russian_industry_label}.`, если она определена. Имеется name → можно кодировать без coarse industry; только code без name → допустим лишь если `industry_for` дал описательный отраслевой текст. Ни name, ни понятной отрасли → omit. Name только из observed profile; недостающий ОКВЭД не угадывать.
- finance: только flags строго True, в порядке loan, leasing, guarantee, factoring, deposit, rko, payroll. Названия: кредитование, лизинг, банковские гарантии, факторинг, депозиты, расчётно-кассовое обслуживание, зарплатный проект. Фраза `В записи банка отмечены продукты: …`. Topics: loan→условия нового кредитования; leasing→условия лизинга; guarantee→условия банковских гарантий; factoring→условия факторинга; deposit→условия размещения средств; rko→условия расчётного обслуживания; payroll→условия зарплатного обслуживания. `Темы для проверки: …`. Не писать про долг, ликвидность, валюту, плавающую ставку, использование чужих банков. False/None не создают отдельный текст. При отсутствии True omit.
- trade_geo: `Регион регистрации: {region}.` если есть; `В записи отмечено участие во внешней торговле.` если fts_present True и не conflicting; `Указанные страны ВЭД: {sorted names}.` только если countries quality observed и FTS не False. При неизвестном FTS — формулировка «В записи указаны страны …; участие требует уточнения», status limited. Нормализованные CN/RU/TR/KZ раскрыть в русские названия по versioned labels; `UNKNOWN:*` и конфликтующие country sets в text не включать. Если качество всего countries unknown из-за смешанного списка, в v1 весь список исключить, unknowns сохранить; не исправлять его молча. Есть только регион → view допустим. Есть только FTS=True → короткий содержательный trade view допустим. Нет этих фактов → omit. Никакого «импортёр», валюты расчётов или территории операций.
- Universal: concatenate base texts в порядке business, finance, trade_geo через один пробел. Те же facts/topics/refs, новая kind/template version; не summary через LLM.

Если итоговое company text вместе с prefix >512 tokens, encoder возвращает InputTooLong. В v1 нет молчаливого truncation. Builder сохраняет ошибку view и не публикует complete snapshot до разрешения; добавить новую версию шаблона можно через review. Длинные события chunk-ятся отдельно в R3.

## 3. Платёжный индекс и контекст

`PaymentDatasetManifest = {source_path, source_sha256, policy_version, currency: str|null, period_start: str|null, period_end: str|null, available_at: timestamp|null, metadata_source: {path, locator, sha256}|null}`. RUB и 2026-01-01/2026-10-31 берутся из явно supplied Q&A manifest, не подставляются для любого CSV. available_at нельзя получить из конца периода. Даты операций неизвестны.

`PaymentEdge = {edge_id, payer_id, beneficiary_id, amount_raw, payment_count, source_record_numbers}`. amount_raw — Decimal string >0; count integer>0. edge_id=hash(payer_id, beneficiary_id, edge_policy_version), только одна сохранённая запись пары. self-loops исключены; любые повторы пары исключаются целиком, в том числе один invalid из дублей. Данные об исключениях и исходные строки сохраняются отдельно.

`Neighbor = {company_id, direction, edge_id, amount_raw, payment_count, observed_share, profile_status, business_view_content_hash: str|null, profile_version: str|null, feature_refs}`. profile_status = usable | missing | conflicting. Доли рассчитаны от ВСЕХ сохранённых соседей данного направления, включая unknown; между направлениями не смешивать.

`DirectionCoverage = {retained_neighbor_count, usable_profile_count, business_vector_count, retained_amount_raw, usable_profile_amount_raw, vector_backed_amount_raw, known_node_fraction, known_amount_fraction, vector_node_fraction, vector_amount_fraction, retained_payment_count, known_payment_count, cleaning_coverage, share_quality}`. Доли null при нулевом знаменателе. A заполняет profile coverage; поля business_vector/vector_* до B равны null, B добавляет реальное покрытие доступными векторами. Нельзя приравнять profile_known к наличию vector.

`PaymentNeighborhood = {company_id, dataset_manifest, graph_content_hash, provenance_hash, incoming: list[Neighbor], outgoing: list[Neighbor], coverage: {incoming,outgoing}, limitations}`.

- graph_content_hash: canonical sorted endpoints, суммы/counts, statuses, local cleaning diagnostics, economic metadata/policy. Не входят batch size, aggregate scan statistics, время импорта, номера строк, полный source hash и состав других несвязанных компаний.
- provenance_hash: исходный CSV/hash/records/manifest; изменяется при переэкспорте источника, даже если граф семантически тот же.
- Отдельный graph_embedding_key учитывает business vector keys соседей; смена их деятельности инвалидирует соответствующие агрегаты, даже если суммы не изменились.

`SectorAggregate = {direction, okved_code, okved_name, neighbor_count, amount_raw, payment_count, observed_amount_share, neighbor_ids, edge_ids, neighbor_feature_refs}`. Не включать unknown в фиктивный ОКВЭД. Отдельное поле unknown totals обязательно. Один сосед с двумя направлениями участвует один раз в каждом направлении.

Будущие API A:

```python
build_company_views(profile: dict, *, policy: ViewPolicy) -> CompanyViews
build_universal_view(views: CompanyViews, *, policy: ViewPolicy) -> CompanyView | None
PaymentGraphStore.build(csv_path, *, output_path, metadata: PaymentDatasetManifest) -> GraphBuildReport
PaymentGraphStore.open(path) -> PaymentGraphStore
PaymentGraphStore.neighborhood(company_id: str, *, repo: CompanyRepository) -> PaymentNeighborhood
PaymentGraphStore.affected_companies(changed_company_ids: set[str]) -> set[str]
build_sector_aggregates(neighborhood: PaymentNeighborhood, *, repo: CompanyRepository) -> list[SectorAggregate]
```

A не кодирует тексты и не знает численных значений векторов. B дополняет coverage, а не модифицирует объект A на месте.

## 4. Encoder, vectors, graph aggregation

`EncoderConfig`: model_id, revision, backend, precision, max_tokens=512, batch_size, threads, device_policy. CPU default: pinned `intfloat/multilingual-e5-small`, revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`, ONNX INT8 из экспериментального manifest, batch=1, intra_threads=2. Подтверждать hashes файлов, не полагаться только на имя.

`EncoderFingerprint`: model/revision, weights SHA, tokenizer SHA/config, dimension=384, masked_mean_pooling_version, l2_normalization_version, query/document prefixes, text_normalization_version, truncation_policy=reject, backend/runtime versions, precision, provider, batch/padding policy и hardware execution class. SHA от canonical dict — `encoder_fingerprint`. Изменение CPU/GPU/INT8/FP32/batch policy = другой fingerprint; автоматически не смешивать.

```python
class EmbeddingEncoder(Protocol):
    fingerprint: str
    def encode_queries(self, texts: Sequence[str]) -> np.ndarray: ...   # (n,384) float32
    def encode_documents(self, texts: Sequence[str]) -> np.ndarray: ...
    def token_lengths(self, texts: Sequence[str], *, input_kind: str) -> list[int]: ...

class BusinessVectorLookup(Protocol):
    def get_business(self, company_id: str) -> EncodedView | None: ...
```

EncodedView: company_id, kind, view_content_hash, vector_key, encoder_fingerprint, vector_sha256, feature_refs, profile_version, metadata_ref. `vector` — отдельное поле runtime ndarray shape(384,), не в JSON. Все значения finite, норма в пределах 1±1e-5; нулевая норма — ошибка, а не нормальный вектор.

Graph vector отдельно имеет recipe_version, direction, weight_policy, graph_content_hash, input_vector_refs, coverage, normalized_weights_ref, norm_before_normalization, limitations. Нет fake text_hash у центроида: representation_type=aggregation; текстовый hash и recipe hash — разные поля.

```python
aggregate_graph_views(neighborhood: PaymentNeighborhood, *,
                      lookup: BusinessVectorLookup, policy: GraphPolicy) -> GraphViews
```

GraphViews: views dict[graph_incoming|graph_outgoing, EncodedGraphView], omitted reasons, updated coverage. Input neighbors sorted by company_id; lookup возвращает query-business vectors только одного fingerprint. Нет вектора соседа → не заменять нулём и не считать его известной частью агрегата.

Для направления d, S — сумма ALL retained amount в этом направлении, K — соседи с business vector:

- equal: raw weight=1;
- amount_share: raw weight=amount_j/S;
- sqrt_share: raw weight=sqrt(amount_j/S).

Вычисления веса/суммы float64, сохраняемый результат float32. a_j=w_j/sum_K(w); z=sum_K(a_j*v_j); output=z/||z||. K пуст или ||z||<1e-12 → omit с причиной, не кодировать «нет соседей». При одинаковых входных векторах можно сначала сложить веса, но sqrt применяется к каждому ребру ДО объединения одинаковых текстов. Сохранять полное соответствие company→edge→weight→business key. Нельзя выкинуть provenance повторов вместе с числовой дедупликацией.

Усреднение не является обученной E5-операцией. Для фиксированной компании dot(z,q)=среднее сходств соседей; L2 на z меняет общий масштаб, не восстанавливает потерянный редкий аспект. При разнородных соседях average может проигрывать поиску отдельными отраслевыми запросами. Direction хранится отдельно, модель не научена ему этим averaging.

Эксперимент graph_combined = normalize(g_in + g_out) при обоих; один доступен → его копия; ни одного → omit. Это равный вес направлений, не сумма рублей; double role одного соседа явно записывается. Не включать в default policy. Hub penalty выключен; global degree доступна в graph store для будущей отдельной абляции.

## 5. Cache, checkpoint и публикация

Директория `data/processed/retriever/company_embeddings/` должна быть ignored. Не включать реальные профили, векторы, веса и новостные тексты в Git.

- `objects.sqlite`: append-only committed vector objects. PK text vector key = H(encoder_fingerprint, input_kind, normalized_text_hash). Shared vector не имеет company-specific provenance. Для graph key = H(encoder_fp, recipe, sorted input vector keys + effective weights + direction); semantic graph hash/coverage/version остаются также в mapping и metadata. Изменение неизвестных связей обновляет metadata даже при прежнем нормированном vector.
- `work/<build_id>.sqlite`: immutable build config, per-company views/refs, omissions, errors, graph dependencies; checkpoint внутри транзакций. PK `(company_id, kind, variant_policy)`; metadata refs не overwritable в published snapshot.
- `snapshots/<snapshot_id>/mapping.sqlite + manifest.json`: опубликованные refs на objects.sqlite, checksums таблиц/файлов, полный список vector object keys/hashes, source manifests, policy versions, counts, build timing и hardware. Snapshot ID по canonical sorted логическим записям/config/source manifests, без времени запуска, импортных timestamps и физических SQLite checksums. Checksums файлов служат проверке целостности и не определяют semantic identity: fresh/resume могут иметь разную раскладку SQLite pages при одинаковых логических данных. Глобальный objects.sqlite не хешируется целиком в snapshot manifest: он append-only растёт, проверяются referenced object hashes. Cache GC в MVP отсутствует; referenced objects никогда не удаляются.
- Предупреждение: перенос snapshot без referenced objects не самодостаточен. Будущая команда verify проверяет все refs. Для переноса builder/export копирует все referenced objects в отдельную sqlite рядом с manifest и меняет storage locator, не ключи векторов.
- Один writer build/cache в MVP; SQLite transaction и exclusive build lock. При параллельном запуске явная BuildLocked, не независимые писатели. Readers видят только committed vectors и published mapping.
- Порядок: compute → commit new vector object → commit metadata/checkpoint. Crash между ними оставляет неиспользуемый object, который можно переиспользовать. Обратный порядок запрещён. Chunk <=128 компаний, inference batch — отдельная настройка, CPU по умолчанию 1.
- Resume проверяет source hashes/config/encoder/template/graph policy; несоответствие = ResumeMismatch, не продолжение старого build. Новый build на новой версии источника reuse-ит vector objects по text keys. Dirty partial snapshot не публикуется. Ошибки views не скрываются под omitted.
- После верификации build создаётся temp snapshot dir, fsync, atomic rename, затем atomic CURRENT pointer. Readers pin snapshot на запрос; прошлый snapshot остаётся доступен. При rebuild удалённые/ставшие конфликтными IDs отсутствуют в новом mapping; object cache может хранить их старые векторы.
- Изменение финансового поля → новый finance text key; business тот же. Изменение ОКВЭД B → новый business B и aggregates компаний, связанных с B. Обратные зависимости даёт graph store. Новые unknown payments меняют coverage независимо от совпадения конечного vector. Source SHA/строки обновляют provenance, не запускают encode сами по себе.

Будущие API B:

```python
VectorCache.get(vector_key: str) -> np.ndarray | None
VectorCache.put(vector_key: str, vector: np.ndarray, *, metadata: dict) -> None
build_company_snapshot(repo, *, encoder, cache, graph_store=None,
                       config: BuildConfig, company_ids: Sequence[str] | None=None,
                       resume=False) -> BuildReport
CompanyEmbeddingSnapshot.open(path, *, cache) -> CompanyEmbeddingSnapshot
CompanyEmbeddingSnapshot.views(company_id: str, *, enabled_views: Sequence[str]) -> list[EncodedView]
CompanyEmbeddingSnapshot.verify() -> VerificationReport
```

При одинаковом key существующий object не переписывать. Shape/bytes/checksum mismatch = CorruptVector/VectorCollision. Истинное изменение backend создаёт новый fingerprint. Target scope и dependency-only neighbors перечисляются отдельно: neighbor business objects доступны агрегатору, но не делают неполный профиль соседа автоматически обслуживаемым target в query API. Полный build не удерживает все CompanyProfile с большим features в RAM: repo уже держит raw rows, normalize/get и write — порциями; дополнительные vector matrices per edge не материализовать.

## 6. Query port и handoff

```python
load_company_queries(snapshot: CompanyEmbeddingSnapshot, *, company_id: str,
                     expected_encoder_fingerprint: str, policy: QueryPolicy,
                     as_of: str) -> CompanyQuerySet
search_dense(index: EventDenseIndexPort, queries: CompanyQuerySet, *, top_k=50) -> list[DenseHit]
```

CompanyQuerySet: company_id, profile_version, snapshot_id, queries (view/vector refs), omitted, payment_context_ref, temporal_mode, limitations. Неизвестный company_id → CompanyNotFound, существующий с нулём views → empty queries. Отключённый graph не требует загрузки payment DB. Expected fingerprint сравнивается с индексом событий; несовпадение не разрешается по совпадению размерности.

`EventDenseIndexPort` от оркестратора: fingerprint, index_snapshot_id, `as_of`, `window_start`, `window_end`; `iter_blocks(max_rows) -> (float32 matrix[n,384], list[ChunkRef])`; `resolve_snapshot(ref) -> StoredEvent`. Index предоставляет только eligible snapshots; C всё равно проверяет fingerprint/as_of и refs. ChunkRef = snapshot key (event_id, integer version, payload_sha256), chunk_id, compound claim_refs, offsets. One event latest snapshot per index. Векторы normalized, finite. Mock port разрешён только tests.

DenseHit: snapshot key, channel=dense, view_kind, view_ref, chunk_ref, rank, raw_score, company_feature_refs, graph_evidence_ref|null. max по chunks события для каждого view; top50 per view, event_id tie-break. Никакого превращения cosine в importance. Runtime JSON не содержит vectors.

Graph provenance в handoff — через `company_context.payment_context`: scoped соседские профили/feature refs, edge evidence, coverage, периоды. Нельзя писать ОКВЭД соседа в verified_facts самой компании. В E0 оркестратор уточняет правило исходного контракта «чужие feature IDs запрещены»: допустимы только явно зарегистрированные neighbor refs с проверенным edge и snapshot, все остальные чужие refs по-прежнему отвергаются. Внешняя schema_version не меняется, в существующих свободных payment_context/trace полях фиксируются shape и validators. Никакого второго CandidateSet.

Strict temporal mode требует подтверждённого available_at графа <=as_of и period_end<=as_of; неизвестный available_at или будущий период выключает graph с объяснением. Diagnostic static_context разрешён только evaluation, помечается во всех отчётах и не меняет временные проверки самих B6 событий. Graph model не делает историческую реконструкцию из агрегата.

Ошибки E0: InvalidProfileReference, InputTooLong, EncoderUnavailable, EncoderMismatch, CorruptVector, VectorCollision, ResumeMismatch, BuildLocked, CompanyNotFound, DependencyNotReady, TemporalContextUnavailable. Существующие ошибки EventStore/B6 не переименовывать.
