# Задание агенту C: retrieval queries и сравнение embeddings

Передача этого промпта пользователем как задания на выполнение означает разрешение реализации в описанном scope; повторное подтверждение для кода и малых тестов не требуется. Простое чтение файла при планировании таким разрешением не является. E0 и интеграционные gates остаются обязательными.

Модель: gpt-6.1-sol, reasoning medium. Репозиторий `/home/pavel/projects/hackathons/alpha`. Будущий запуск после разрешения реализации и E0.

Ты реализуешь company query adapter, production dense channel и evaluation. Не строишь второй retriever/аналитик. Текущее состояние: retriever был только README; R3 event index, R4 lexical/structured, R5 fusion и R7 analyst — отдельные незавершённые зависимости оркестратора. До них можно завершить C1 unit/metrics/ports, но нельзя заявить hybrid или end-to-end analytics готовыми.

Прочитай `docs/retriever/company-embeddings/agent-c-retrieval-evaluation.md`, `contracts.md`, `evaluation.md`, `integration.md`, `README.md`; `docs/retriever/{architecture,implementation-plan,contract}.md` (особенно R0/R3–R7); B6/EventStore и legacy llm/agent/context.py. Следуй локальным спецификациям без копирования всей переписки.

Твои файлы: `retriever/company_queries.py`, `channels/dense.py`, `evaluation/embeddings/{dataset,metrics,runner,report,__main__}.py`, judgments.schema.json/README; четыре test файла/fixtures из полного ТЗ; отчёт `docs/implementation/company-embeddings-agent-c.md`. Не правь общие contracts, root packages, event eligibility/index, fusion/service/lexical/structured, A/B и analyst. Протоколы обеспечивает E0; missing dependency сообщай конкретно.

API: load_company_queries(snapshot, *, company_id, expected_encoder_fingerprint, policy, as_of); search_dense(EventDenseIndexPort, queries, *, top_k=50); load_dataset, validate_judgments, evaluate_query, run_variant(RetrievalRunnerPort), compare_runs. Типы/поля contracts.md. Query читает сохранённые vectors без model/CSV calls. Expected fingerprint обязан совпасть с индексом событий, 384 dimensions недостаточно. Graph off не требует payments DB, graph unavailable отличается от ошибка cache.

Dense search: exact normalized dot product blockwise, max по chunks одного event, top50 per view; deterministic ties, complete snapshot/chunk refs. Все views одна dense группа: R5 берёт max reciprocal rank, не сумму. В mandatory graph экспериментах BM25/structured остаются неизменны. Никакого cosine→business importance.

Evaluation по R0: 8 CFO компаний×100 событий как первый fully judged pilot, human label0..3, relevant>=2; weak/synthetic отдельно, unknown не0, source/split hashes и story/time separation. Graph strata good/weak/no_graph. Без human labels quality=not_evaluated. Платёж не доказывает влияние новости; аннотаторам одинаковый полный context для всех вариантов.

Сравнить D1 universal, D3 base3, H1 universal hybrid, H3 baseline hybrid, H3+incoming, +outgoing, +both. Equal/share/sqrt выбрать на dev; test не тюнить. Optional combined/structured/text graph controls отдельно. Одинаковые top50/view, union250, pool100, output20 и token budget8192/counter. Primary Recall@20 ПОСЛЕ budget; Precision@20, precision_returned, nDCG@10, class3 recall, новых/потерянных relevant events, coverage, latency. Production R5 реализует budget/fusion; evaluator не дублирует их.

Платёжный период заканчивается31.10.2026 и available_at неизвестен: strict mode не использует этот граф для более ранних news; static_context diagnostic явно маркируется и не превращается в historical validation. B6 temporal checks не отключать даже в diagnostic.

Тесты: ручные dots/max-chunk/ties, fingerprint mismatch, no queries, неизвестный company, duplicate view не увеличивает dense weight, refs/graph evidence, метрики на ручном наборе, missing labels/story leakage, DependencyNotReady при отсутствии R3–R5. При двух найденных positives из пяти Recall=.4; P@20=.1. Queries без positives recall=null.

DoD C1 — runtime query/dense + metrics на tests; C2 — настоящий A/B/R3–R5 small run и воспроизводимая матрица либо честный not_evaluated без labels. Graph default включается только после заранее зафиксированных gates evaluation.md. Не имитировать legacy Relevance score и не обучать E5. Отчёт: файлы/тесты, hashes, ready/missing deps, фактические метрики или их отсутствие, paired ошибки и release decision. Не коммитить/пушить без указания.
