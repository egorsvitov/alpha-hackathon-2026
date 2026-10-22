# Задание агенту B: E5 encoder, cache, resume, graph vectors

Передача этого промпта пользователем как задания на выполнение означает разрешение реализации в описанном scope; повторное подтверждение для кода и малых тестов не требуется. Простое чтение файла при планировании таким разрешением не является. E0 и интеграционные gates остаются обязательными.

Модель исполнения: gpt-6.1-sol, reasoning medium. Репозиторий `/home/pavel/projects/hackathons/alpha`. Выполняй только после отдельного разрешения реализации и готового E0; сейчас документ служит будущим промптом.

Контекст: Impact Retriever ищет новости для выбранной компании. E5-small 384 без обучения: company `query:`, event `passage:`. MVP до трёх base views; graph_incoming/outgoing опциональны и default off. Кэш компаний — НЕ индекс событий.

Прочитай `docs/retriever/company-embeddings/agent-b-encoding-storage.md`, `contracts.md`, `integration.md`, `README.md`, а также experimental `ranking/experiments/cpu_news_embeddings/encoder.py`, DIMENSIONS_RESULTS.md и `docs/retriever/architecture.md`. Полный контракт находится локально; копировать историю чата не нужно.

Твои файлы: `retriever/embeddings.py`, `embedding_cache.py`, `company_build.py`, `graph_embeddings.py`, `embeddings_gpu.py`, `company_build_cli.py`; tests `test_embeddings`, `test_embedding_cache`, `test_company_build`, `test_graph_embeddings` и synthetic fixtures `retriever/tests/fixtures/embeddings/*`. Отчёт `docs/implementation/company-embeddings-agent-b.md`. Root pyproject/lock/notices/ignore, dispatch/общие contracts и файлы A/C не менять; необходимые изменения передать оркестратору.

API: EmbeddingEncoder encode_queries/encode_documents/token_lengths/fingerprint; VectorCache.get/put; build_company_snapshot; CompanyEmbeddingSnapshot.open/views/verify; aggregate_graph_views. Точные fields/signatures contracts.md. Вход A — CompanyViews/PaymentNeighborhood; до поставки A работай на E0 fixtures, не дублируй нормализацию компаний.

CPU first: pinned multilingual-e5-small revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`, официальный ONNX INT8 из проверенного manifest, masked mean pooling и L2. Output finite float32 shape(n,384). Prefix только в encoder, один раз. >512 tokens ошибка, не silent truncation. CPU inference batch1/thread2; processing chunks128 — отдельное понятие. Модель грузится один раз; сетевые downloads только явный setup.

Cache: vector key по fingerprint/input kind/normalized text hash; identical text→один encode, provenance каждого company отдельно. SQLite object store immutable committed BLOBs, отдельный work checkpoint и immutable published mappings. Сначала object commit, потом metadata/progress. Resume требует совпадения source/config; changed source→новый build с reuse objects. atomic snapshot/CURRENT, checksums, все refs проверены. Full artifacts ignored, никаких реальных CSV/weights в Git.

Graph: отдельные normalized weighted means business query vectors соседей; equal/share/sqrt_share доступны, default experiment sqrt без утверждения о лучшем качестве. Нет пригодных vectors→omission. Отдельно known-profile/vector coverage. После L2 coverage не терять. Суммировать float64, хранить float32; sqrt каждого ребра до объединения identical vectors. Все input IDs/edge refs/weights сохраняются. Смена business соседа инвалидирует dependent graph, finance соседа — нет.

GPU optional: подходящий FP32 backend и отдельный fingerprint, не запуск VNNI на CUDA заменой строки provider. CPU fallback до build; mid-run GPU failure не смешивает CPU vectors в прежнем snapshot. Event index/query fingerprints обязаны совпадать. GPU без hardware помечается skipped; не мешает CPU DoD.

Тесты обязательны: shape/norm/prefix, duplicate text dedup, fingerprint mismatch, source-only provenance update, crash/resume на всех commit/publish границах, zero extra encode после complete resume, corrupt/missing objects, writer lock, ручные graph means/unknown90%/cancellation. Real CPU smoke <=32 texts после authorization; full data не запускать до I4. Не обучать E5, не вызывать LLM, не создавать ANN/vector service и fake ranking score.

DoD: опубликованный snapshot читается в новом процессе; unchanged text не кодируется повторно; graph default off; provenance/coverage полны; CPU и failure tests проходят. Сдать report с exact tests, timings только измеренными, hardware/backend, GPU tested/skipped, остаточными зависимостями. Не пушить/коммитить без отдельного указания.
