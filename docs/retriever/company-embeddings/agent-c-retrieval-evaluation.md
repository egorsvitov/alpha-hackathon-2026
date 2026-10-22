# Агент C — company queries, dense search и оценка

Модель: Codex Sol 6.1 Medium. Будущее задание после E0 и отдельного разрешения execution. Прочитать [contracts.md](contracts.md), [evaluation.md](evaluation.md) и [integration.md](integration.md). Цель — использовать company snapshots как запросы к событиям и измерить эффект views, не создавать второй несовместимый retriever.

## Текущее состояние и границы

R0 — документированный протокол, а не готовый gold dataset. R3 event index, R4 lexical/structured и R5 fusion ещё не реализованы. Их предоставляет оркестратор по существующему общему плану. C реализует production dense channel и evaluation; до появления других каналов разрешены unit/smoke и dense-only измерения, **не выдуманный hybrid результат**.

Работа делится на C1 (контракты запросов, метрики, dense tests) и C2 (живой eval после интеграционных зависимостей). При недоступных R3–R5 предоставить готовый C1 и machine-readable DependencyNotReady, а не расширять scope самовольно.

## Прочитать

`docs/retriever/{architecture,implementation-plan,contract}.md`, R0/R3/R4/R5/R6/R7; весь пакет `docs/retriever/company-embeddings/`; `ranking/{b6,contracts,event_store,entity_mapping}.py`; B6 v0.2 реальные схемы/fixtures; `llm/agent/context.py` для понимания несовместимости legacy. Existing CPU artifacts — только данные корпуса/benchmark, не готовая новая vector matrix.

## Владение

Разрешено создавать:

- `retriever/company_queries.py`;
- `retriever/channels/dense.py`;
- `retriever/evaluation/embeddings/{dataset.py,metrics.py,runner.py,report.py,__main__.py,judgments.schema.json,README.md}`;
- `retriever/tests/test_company_queries.py`, `test_dense_channel.py`, `test_embedding_metrics.py`, `test_embedding_eval_runner.py`;
- `retriever/tests/fixtures/embedding_evaluation/*` (синтетические небольшие данные);
- `docs/implementation/company-embeddings-agent-c.md`.

Оркестратор владеет package init, `retriever/contracts.py`, representation contracts/ports, `index.py`, `event_views.py`, `eligibility.py`, lexical/structured channels, `fusion.py`, `service.py`, `__main__.py`, root metadata. Не создавать собственные копии этих компонентов в evaluation. Не редактировать A/B files и legacy analyst.

## Точные API

Production:

- `load_company_queries(snapshot, *, company_id, expected_encoder_fingerprint, policy, as_of) -> CompanyQuerySet`;
- `search_dense(index: EventDenseIndexPort, queries, *, top_k=50) -> list[DenseHit]`.

Evaluation:

- `load_dataset(manifest_path) -> EvaluationDataset` — checksums, refs, splits, scope;
- `validate_judgments(dataset, judgments) -> JudgmentValidationReport`;
- `evaluate_query(selected_event_ids, judgments, *, k=20, relevant_min=2) -> QueryMetrics`;
- `run_variant(dataset, *, retrieval_port: RetrievalRunnerPort, config: VariantConfig) -> VariantRun`;
- `compare_runs(runs, *, bootstrap_seed=42, bootstrap_samples=2000) -> EvaluationReport`.

E0 задаёт `RetrievalRunnerPort.run(request, *, company_queries, config) -> RetrievalResult`; реализация вызывает production R4/R5, тестовая заглушка только в tests. runner не делает свою fusion/filter/budget. Допустим отдельный dense-only режим с production search_dense и тем же R5 budget, честно маркированный.

## План реализации

1. Проверить E0; сделать fixtures index port и snapshot port, не требующие загрузки модели/платежей. Реализовать схемы judgments и ручные metric tests по R0.
2. load_company_queries читает опубликованный snapshot B, проверяет company existence, fingerprint и integrity. По default возвращает доступные base3; отсутствующие views — не ошибка. Graph требует flag+recipe+temporal applicability. Нет загруженного graph при graph off — обычный режим, не degraded.
3. Сохранить view refs/feature refs/graph context refs, но raw vectors не класть в JSON trace. Чтение CompanyRepository/CSV/model inference на запросе не допускается.
4. Dense search: dot product normalized vectors по блокам event matrix. Для каждого view максимум по chunks одного event; stable sort по score desc, event_id asc; top50. При ties winning chunk — stable chunk_id. Версия/hash входят в snapshot key. Если несколько versions одного event в index — ошибка index invariant, не самостоятельное решение C о latest.
5. Проверить as_of/window/fingerprint до поиска. Index уже прошёл R3 eligibility; бракованный/mock index не означает право пропускать R3 в реальном сервисе. Empty queries → пустой dense channel; все каналы failed решает R5, не возвращать success empty из evaluator.
6. Передать результаты в production fusion. Dense views — одна группа. max reciprocal rank по views, не сумма всех graph/base запросов. Переход 3→5 views не увеличивает coefficient dense. Сохранять все кандидаты, победившие views/chunks и причины omitted после бюджета.
7. Подготовить R0 pilot manifest: 8 CFO profiles, 100 уникальных story/event candidates, исходные hashes и provenance; выбрать/обозначить graph strata. Synthetic профили/события изолировать, не выдавать за production coverage. Реальные данные остаются ignored; в Git только схемы и synthetic test fixtures.
8. Judgments: 0–3 и unknown/not_judged; valid refs, human/weak отдельно; один fixed information context для всех вариантов. Пока human labels нет, заполнить форму и report `quality_status=not_evaluated`, не ставить метки от себя как human. Manual metric fixtures допустимы, но не входят в quality gold.
9. Реализовать матрицу D1/D3/H1/H3/H3+in/+out/+both из evaluation.md. Настройки E5/templates/corpus/as_of/token budget фиксированы. Для graph сравнения BM25/structured inputs неизменны. Дополнительный structured graph вариант отдельный; в required H3+graph он не включается.
10. Метрики считать по уникальным logical events после R5 budget. Recall relevant>=2; class3 отдельно; Precision@20 с denominator20, дополнительно precision_returned; nDCG@10 gain 2^label-1. Unknown не считать нулём. Некомплектная разметка => pooled/partial report, не full recall и не promotion.
11. Сравнить paired per query gains/losses, strata, macro/micro, coverage, duplicates, latency p50/p95, indexing/build times отдельно. Bootstrap cluster по company_id; маленькие наборы помечать exploratory. Budget tokens входят по одной и той же версии counter, не заменять его своим в отдельных вариантах.
12. В C2 провести integration через реальные A/B и R3/R4/R5, validate ImpactCandidateBatch refs/hash. Не создавать fake Relevance для аналитика. Статус R7 pending до настоящего нового context builder оркестратора.
13. Подготовить воспроизводимую CLI/report; graph_release_decision = keep_base3 | evidence_supports_graph | insufficient_evidence. Заключение об улучшении возможно только при gates evaluation.md и подходящем temporal mode.

## Проверки

- Неизвестная компания vs company без views различаются; disabled graph не открывает graph DB; отсутствующий enabled graph отражается в omissions.
- CPU/GPU fingerprint mismatch, shape384 mismatch, nonfinite/corrupted vector отвергаются.
- Ручная матрица: dot/cosine, chunk→event max, dedup, tie order. Дополнительный одинаковый chunk не повышает score. Одинаковый view, продублированный под другим именем, не увеличивает fused dense contribution (integration с R5).
- Query retrieval не вызывает encoder или CSV reader; можно mock-нуть их на исключение.
- Wrong snapshot hash/foreign feature/no edge to neighbor → invalid reference в handoff; source claims/conflicts/negation не меняются. C не реализует собственный B6 validator.
- `as_of` future/unknown graph metadata: strict omit; static diagnostic flag явный; B6 future event всё равно запрещён.
- Метрики: 2 relevant из 4 найденных при 5 relevant всего → Recall=.4, P@20=.1, precision_returned=.5; query без positives recall=null; missing judgment не label0; duplicate output IDs не увеличивают hit count; IDCG0→nDCG=null.
- Story split collision, differing source hashes/configs, changed test labels, смешение role, weak-as-human → evaluator отказывается от сопоставимого report.
- R3–R5 отсутствуют → DependencyNotReady с именами модулей, не stub hybrid score. Без labels smoke остаётся schema/runtime validation.

## DoD

C1: production dense adapter + metrics/tests готовы на ports, все ограничения enforced. C2: хотя бы один реальный small end-to-end run через A/B/R3–R5 с исходными B6 bundles и audit trace; матрица evaluation воспроизводима, но качество может честно оставаться not_evaluated. Promotion graph не является обязательным положительным исходом.

Зависимости: E0 → C1; A+B+R3–R5 → C2; human gold → quality conclusion; R7 → actual analyst/API. Не создавать дополнительные сервисы, продвинутые модели, thresholds по test, или обучать E5.

Отчёт `docs/implementation/company-embeddings-agent-c.md`: files, tests, exact dataset/split/config hashes, dependencies ready/missing, actual metrics или not_evaluated, example new/missed relevant events, timings/memory, release decision и основания. Не смешивать mock/human/synthetic/live.
