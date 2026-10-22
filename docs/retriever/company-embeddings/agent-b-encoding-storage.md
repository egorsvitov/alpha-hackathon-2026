# Агент B — encoder, хранение и графовые векторы

Модель: Codex Sol 6.1 Medium. Будущее задание после E0 и отдельного разрешения реализации. Спецификация — [contracts.md](contracts.md). Цель: воспроизводимый CPU-путь CSV views → shared vector objects → published company snapshot; optional GPU и graph без смешения пространств.

## Изучить

`docs/retriever/company-embeddings/{README,contracts,integration,evaluation}.md`; `docs/retriever/architecture.md`; R2/R3/R8 общего плана; `ranking/experiments/cpu_news_embeddings/{encoder.py,benchmark.py,DIMENSIONS_RESULTS.md,pyproject.toml,uv.lock}`; `ranking/profiles.py`; A fixtures после его поставки. CPU experimental vectors не импортировать как новый production snapshot: там был другой query/document direction.

## Файлы, которыми владеет B

Создавать `retriever/embeddings.py`, `embedding_cache.py`, `company_build.py`, `graph_embeddings.py`, `embeddings_gpu.py`, `company_build_cli.py`; `retriever/tests/test_embeddings.py`, `test_embedding_cache.py`, `test_company_build.py`, `test_graph_embeddings.py`; `retriever/tests/fixtures/embeddings/*`; отчёт `docs/implementation/company-embeddings-agent-b.md`.

Общий `__main__.py` меняет оркестратор, B предоставляет `company_build_cli.register_parser()` и обработчики. Root pyproject/uv.lock/.gitignore/THIRD_PARTY_NOTICES и contracts не трогать: предложить точные изменения оркестратору. Не менять CPU-эксперимент, A modules, C evaluation или source CSV.

## Контракты и API

Реализовать EmbeddingEncoder (`encode_queries`, `encode_documents`, `token_lengths`, fingerprint), VectorCache.get/put, `build_company_snapshot`, CompanyEmbeddingSnapshot.open/views/verify, `aggregate_graph_views` по contracts.md. `BusinessVectorLookup` реализовать поверх published/work mapping; graph computation начинается только после готовности всех нужных business views.

BuildConfig точен в E0: output_root, company_ids_scope, view_policy_version, graph_policy, encoder_config, company_chunk_size=128, temporal_mode, source manifests. Ни inference batch, ни output dtype не выбирать скрыто.

## Пошаговая реализация

1. Проверить E0 types, утвердить dependency list у оркестратора. CPU first без GPU dependencies в обязательном окружении. Не менять requires-python всего монорепозитория ради одного пакета: если current root 3.13 не совместим, сначала вынести вопрос/вариант optional extra оркестратору.
2. Выделить production encoder, переиспользуя алгоритм экспериментального: pinned model/tokenizer, attention-mask mean pooling, float32 output, L2. Не импортировать экспериментальный main/download side effects. `encode_queries` добавляет ровно `query: `; documents — `passage: `. Пустой list → shape(0,384), пустой/пробельный текст → ошибка. Длины измерять до truncation; input>512 → InputTooLong.
3. Сверять model artifacts с manifest/hashes и approved revision. Загрузка модели/файлов — явный setup шаг; кодирование не скачивает молча новую ревизию. Лицензия MIT и точные пакеты передаются оркестратору для notices.
4. CPU baseline: ONNX Runtime CPUExecutionProvider, INT8, batch1, threads2. Batch processing 128 company records не означает inference batch128. Иное batch_size — отдельный fingerprint и benchmark, поскольку известен quantization/padding drift. Проверять providers и dtype фактического session.
5. Реализовать object cache по ключу из contracts. Vector records immutable, checksum/shape/finite/norm проверяются при put и verify; query/document разные keys даже при одинаковом тексте. Shared text object не содержит company-specific refs. Одинаковый input у N компаний вызывает encoder один раз, metadata N компаний сохраняется отдельно.
6. Реализовать work DB/checkpoint/resume. Stable ordering company IDs; каждый chunk сначала commit vector objects, затем mapping/progress. Сохранять failed company/view отдельно и не публиковать complete snapshot при непредусмотренной ошибке. Omission по политике данных отличается от runtime error.
7. Base build использует один CompanyRepository и `build_company_views` A. Нормализованные профили не накапливать все в RAM. Для пилота graph-enabled selected companies включить в dependency closure business views всех пригодных соседей; finance/trade соседей не вычислять без необходимости. Отчёт различает target companies и dependency-only companies.
8. Graph pass: `aggregate_graph_views` отдельно incoming/outgoing; equal/amount_share/sqrt_share, deterministic float64 reduction → float32 normalize. Требовать один encoder fingerprint входов; недоступные vectors уменьшают vector coverage, а не превращаются в zero. В full complete build ошибка business encode соседа блокирует публикацию, а отсутствие по данным даёт явный omission. Не маскировать failure как штатную неполноту.
9. Vector identity отдельно от metadata identity: изменение неизвестного ребра может не менять normalized vector, но обязано изменить coverage/provenance. Изменение business соседей — invalidation через input vector keys. Aggregate provenance содержит вклад всех исходных company IDs даже при reuse одинакового business vector.
10. Реализовать verify/publish в отдельную snapshot dir с atomic CURRENT. Проверять весь набор refs, metadata foreign keys, object checksums, counts и config. Workdir не открывается как published snapshot. Добавить export с полным набором referenced vector objects для переносимости (в пределах company_build_cli, без второго vector DB).
11. Обновления: новый build manifest/source versions, reuse objects по content; deleted/conflicting company IDs исключаются из текущего mapping. Использовать A reverse adjacency для changed business neighborhoods. Initial implementation может пересчитать все дешёвые graph recipes, но НЕ все unchanged text embeddings; selective recipe update — optimization после correctness. При смене payments учитывать union old/new affected edges.
12. Optional GPU: отдельный backend с подходящим FP32 artifact той же закреплённой модели; кандидат — PyTorch/Transformers masked mean path, dependencies optional и pinned после согласования. Проверить доступность CUDA/device/memory и tiny smoke. Не запускать VNNI-INT8 graph на CUDA по замене provider string. `device=auto` выбирает доступный путь до fingerprint/build. `device=cuda` без подходящего окружения → EncoderUnavailable; `auto` сообщает CPU fallback. Mid-build GPU error → failed resumable build, без CPU продолжения под GPU fingerprint. CPU rebuild — новый build ID. Fingerprint событий и company queries должен совпадать, поэтому GPU experiment требует совместимого event index.
13. CLI handlers будущих команд `company-embeddings build|resume|verify|export|inspect`. Dry-run показывает число scope IDs и планы, не вызывает encode. Отчёт содержит duration этапов, unique texts, encoded texts, cache hits, tokens, omitted/errors, RSS, backend/model/source hashes.
14. Передать C published tiny snapshot + object store, A список несовпадений контракта. Не запускать полный датасет до I4.

## Проверки

Unit без model download: mocked deterministic encoder; checksums, batch resume и graph arithmetic. Real model tests отдельные gated smoke, не выдавать mock за скорость/семантику модели.

- Ручная masked mean проверка с padding; prefix ровно один; пустой/слишком длинный текст; shape/norm/NaN/Inf.
- query/document same text → разные vector keys; backend/precision/revision/policy changes invalidates; unrelated source-row change → metadata update, zero new encode для прежнего текста.
- Десять профилей с одним business text → один encode, десять корректных mappings.
- Инъекция crash: до vector commit, после object commit до mapping, до checkpoint, до publish, после rename до CURRENT. Resume восстанавливает тот же семантический snapshot без повторного кодирования committed inputs. Полностью обработанный resume → 0 encode calls. Orphan committed objects не мешают.
- Неизменный build → тот же semantic manifest; timestamps в telemetry не влияют. Source/config mismatch resume → явная ошибка.
- Corrupt BLOB / missing vector / wrong dim / wrong fingerprint / concurrent writer / незавершённый snapshot → явная ошибка.
- Graph fixture v1=(1,0,…), v2=(0,1,…), amounts 25/75: equal нормализует (1,1); share — (.25,.75); sqrt — (.5,sqrt(.75)). Норма единичная, правильное разделение направлений.
- Unknown сосед с 90% суммы: vector оставшегося 10% нормирован, coverage=.1; сосед без vector не заменяется нулём. K=0→omitted; cancelling test vectors→near_zero_norm.
- Два одинаковых business texts соседей: sum(sqrt(share_i)) не заменить sqrt(sum(share_i)). Provenance содержит обоих.
- Смена business одного соседа обновляет только affected graph mappings/recipes; смена финансов только этого соседа не меняет граф на business vectors.
- Real CPU smoke <=32 short texts: proper providers, finite (n,384), norms. CPU repeat/padding drift измерить, не назначать неизвестную погрешность как quality. GPU tests явно skipped при отсутствии hardware; GPU не заявлять tested.

## Acceptance / DoD

CPU path работает без CUDA и платных API; сохранённый snapshot читается в новом процессе. Repeated complete build/resume имеет 0 inference для unchanged unique texts; не меняет чужой provenance. Все перечисленные failure cases покрыты. Graph default off. Нет silent truncation, silent provider switch и загрузки модели на каждый запрос.

После разрешения I4: pilot <=256 target companies, с graph dependency closure; измерить cold/warm отдельно. Full build только затем; память/скорость не гарантировать по эксперименту на 100 новостях. GPU — дополнительная поставка, не условие готовности CPU MVP.

## Зависимости, запреты и отчёт

E0 обязателен; B может работать на CompanyViews/Neighborhood fixtures параллельно A. Production build использует A, не свою копию шаблонов/CSV normalization. C получает lookup/snapshot API. Не создавать ANN, microservice, paid LLM, GNN, новый handoff или fake ranker. Общий embedding backend потом используется R3 для событий, не дублируется там.

Отчёт `docs/implementation/company-embeddings-agent-b.md`: modified files, dependency requests, fingerprint, exact tests/commands, resume fault matrix, measured encode/cache counts, pilot timings/RSS если разрешён, GPU tested/skipped, remaining gates. Точность рекомендаций пока `not_evaluated`.
