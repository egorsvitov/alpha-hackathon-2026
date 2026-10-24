# Standalone retriever

The retriever orders candidates by exact cosine similarity to supplied company
interests. Similarity is a selection signal; an external analyst determines
business significance. The implementation uses no old B6/NLP pipeline and needs
no GPU or model weights for import, validation, and search.

Install the pinned development dependencies from the repository root, then
generate the public schemas:

```bash
python -m pip install -r retriever/requirements-dev.txt
python -m retriever.schemas --output retriever/schemas
```

Versioned Draft 2020-12 schemas in `retriever/schemas/*.v1.schema.json` are generated
from the public Pydantic contracts. Tests check schema validity and exact drift,
and validate synthetic input/output handoffs. JSON Schema describes structure;
Python validation additionally enforces hashes, vector norms, date precision,
version consistency, and references between objects.

Public imports include `RetrieverService`, `SQLiteSearchStore`,
`ClusterMergeAdapter`, and the input/output models directly from `retriever`.

Run commands from the repository root using the installed Python environment.
Input contracts and vector manifests are described in
[the input format specification](../docs/retriever-input-formats.md).

```bash
python -m retriever validate --articles /local/articles.jsonl --vectors /local/bundle --clusters /local/clusters.json
python -m retriever import --articles /local/articles.jsonl --vectors /local/bundle --clusters /local/clusters.json --store /local/index.sqlite3
python -m retriever validate --store /local/index.sqlite3
python -m retriever inspect --store /local/index.sqlite3
python -m retriever retrieve --store /local/index.sqlite3 --request /local/request.json --interests /local/interests.json --output /local/retrieval
```

`validate` checks input schemas, source/corpus/artifact hashes, vectors, item
bindings, and cluster memberships before transactionally importing to a temporary
memory store. Its alternative `--store` mode validates the persisted active
snapshot. `inspect` prints metadata and active item/vector counts. Missing index
paths are rejected by read commands. Commands print concise JSON summaries;
failures return exit status 2.

`retrieve` writes the complete `RetrievalCandidateBatch` to `batch.json` and the
execution trace to `trace.json`. The request's `max_context_bytes` bounds the
complete serialized batch, including sources and metadata. Trace bytes do not
consume the downstream analyst batch budget. Existing output directories are rejected; use a fresh directory for each run.
Treat these local artifacts as source-sensitive data.

Default retrieval makes zero new LLM calls. Pending clusters require a merge
service; otherwise they are deferred with an explicit reason. `raw_fallback=true`
allows raw members and marks that limitation in a partial batch. Already applied
events remain available without opening a merge service.

To apply an existing cached merge result, supply `--merge-state /local/merge.sqlite3`
and the same optional `--profile /local/profile.json` used to prepare it. A
completed unapplied result also needs an external encoder matching the index's
exact fingerprint and dimension:

```bash
python -m retriever retrieve --store /local/index.sqlite3 --request /local/request.json --interests /local/interests.json --output /local/retrieval --merge-state /local/merge.sqlite3 --profile /local/profile.json --encoder-factory my_encoder:create_encoder
```

`--encoder-factory` imports a trusted local `module:function` and calls it with
no arguments. Its object supplies `fingerprint`, `dimension`, and
`encode_event_texts(texts)` returning a normalized `float32` matrix. It must use
the pinned encoder and text policy represented by the imported fingerprint.
The retriever does not download or substitute a model.

Live retrieval requires all of `--max-calls N` with positive N, `--merge-state`,
`--profile`, `--quota-db`, and `--quota-limit`. The shared quota file must already
exist and contain the initialized quota tables with exactly that limit. Opening
an index never initializes or resets a quota. `--max-calls` explicitly overrides
the request's new-call allowance; a positive request allowance without this flag
is rejected. Credentials come exclusively from `LLM_API_KEY` in the environment;
the retriever CLI does not load `.env` files or accept secrets as arguments.
The provider uses one-shot bounded HTTP calls with no automatic retries.
A deadline prevents new reservations after expiry; an in-flight paid call may
finish after the retrieval deadline.

## Synthetic offline example

```bash
python -m retriever.example --output /tmp/retriever-demo
```

This creates three synthetic articles, ready-to-import manifest and clusters,
interests and request files, an index, and a merge cache. A deterministic fake
provider warms the cache once. Retrieval then applies the cached event with
zero new calls and a deterministic two-dimensional fake encoder. Expected summary:
`ready`, two candidates, `new_calls=0`. Repeating the command reuses local state.
These fake outputs check integration behavior and make no claim about LLM
semantic quality or embedding relevance.

The generated files can also exercise the CLI against a fresh index:

```bash
python -m retriever import --articles /tmp/retriever-demo/articles.json --vectors /tmp/retriever-demo/manifest.json --clusters /tmp/retriever-demo/clusters.json --store /tmp/retriever-demo/fresh.sqlite3
python -m retriever retrieve --store /tmp/retriever-demo/fresh.sqlite3 --request /tmp/retriever-demo/request.json --interests /tmp/retriever-demo/interests.json --output /tmp/retriever-demo/fresh-output --merge-state /tmp/retriever-demo/merge.sqlite3 --encoder-factory retriever.example:encoder_factory
python -m pytest -q retriever/tests llm/cluster_merge/tests
```

The fake factory is suitable only for that synthetic fingerprint.

Python integration is `RetrieverService().retrieve(request, interests=interests,
store=store, merge=adapter, encoder=encoder)`, returning `RetrievalRun` with
`batch` and `trace`. Imports expose `read_articles`, `read_clusters`,
`read_vector_bundle`, and `read_interests`; `SQLiteSearchStore.import_bundle`
atomically creates an immutable initial snapshot. Merge application verifies
full sources and cluster revision under a transaction. Cluster membership changes
invalidate old event applicability and restore originals; overlaps remain
ambiguous. Known publication and observation dates constrain availability;
unknown source/profile availability prevents a historical-validity claim.

## Ручная оценка и завершение интеграции

Сформировать объединённый пул из выдач сравниваемых вариантов:

```bash
python -m retriever.evaluation pool --batches /local/baseline/batch.json /local/lazy/batch.json --output /local/review.json
python -m retriever.evaluation evaluate --batch /local/lazy/batch.json --labels /local/review.json --output /local/metrics.json
```

В `review.json` рецензент заполняет `relevance`, `merge_quality`, `detail_loss`,
при возможности — общий `story_id` для одного события в разных вариантах.
Нужно читать реальные интересы компании и исходные новости, не только пересказ.
Незаполненные метки остаются `null`; это не отрицательные примеры.
`known_positive_pool_recall` считает размеченные объекты пула и может считать
разные представления события отдельно. Для сравнения baseline/lazy используйте
`known_positive_story_recall` с ручными `story_id`. Это полнота только относительно
известных событий пула. Абсолютная полнота поиска по корпусу не измерена.
Файл разметки не перезаписывается командой создания пула.

Выдача CLI теперь публикуется атомарно в **новый** каталог: `batch.json`,
`trace.json`, `manifest.json` с SHA-256 обоих файлов. Существующий каталог
отклоняется до платных вызовов; для следующего прогона укажите новый путь.

Ветка включает каталог интересов, но сами GPU-матрицы, индекс новостей и рабочий
encoder остаются локальными артефактами владельца эмбеддингов. Их нельзя заменить
старыми E5-векторами или BGE-профилем другого окружения без проверки fingerprint.
Старый скрипт `retriever.experiments.embed_activity_interests` теперь получает
`--encoder-factory module:function` для **нового** расчёта, без импортов старых
`llm.event_pipeline` и `retriever.raw_encoder`. Готовый результат по-прежнему
проверяется через `--verify-only` без модели. Фабрика вызывается с `cache_path`
и `batch_size=8` и должна вернуть совместимый encoder с полями `fingerprint`,
`fingerprint_metadata`, `profile`, `metrics`, `cache` и методами `token_lengths`,
`encode_queries`, `vector_key(text, input_kind=...)`. `cache` предоставляет
`writer_lock()` и `close()`. Конкретную модель, её загрузку и этот адаптер
поставляет владелец эмбеддингов. Fingerprint и численная parity обязательны.

Новый пакет — `retrieval-candidate-batch.v1`, а не старый B6-контракт. До
подключения аналитика потребитель должен валидировать его схему, учитывать
`status`, `deferred`, происхождение и ограничения. Бизнес-анализ и итоговая
сортировка не входят в этот модуль.

## Реальный CPU / API прогон и HTML для команды

`CPUEncoder` проверяет хэши весов закреплённого BGE-M3, CLS pooling, отсутствие
префиксов, FP32 и L2. Затем кодирует восемь сохранённых запросов и восемь новостей:
cosine должен быть не ниже 0.99999, максимальная ошибка координат — не выше 1e-4.
Только после проверки encoder получает compatibility alias fingerprint индекса.
Его настоящий CPU runtime fingerprint и протокол проверки сохраняются отдельно:
это численно проверенная совместимость, а не заявление об идентичности CPU/CUDA.
Все новые события проходят этот encoder без обрезания; exact-text CPU-кэш
не смешивается с GPU-кэшем. Модель загружается только из явно указанного локального
каталога. Повторный расчёт всего корпуса не требуется.

Зависимости расчёта/отчёта — `requirements-cpu.txt`. PyTorch устанавливается
отдельно из CPU-индекса; CUDA-зависимости для этой машины не требуются.

```bash
python -m retriever.experiments.live_cpu \
  --root /absolute/project-root \
  --output /absolute/project-root/data/processed/retriever/live-cpu-20261024 \
  --model-path /absolute/huggingface/snapshot/5617a9f61b028005a4858fdac845db406aefb181 \
  --k 5 --max-calls 100
python -m retriever.experiments.report_live \
  --root /absolute/project-root \
  --run /absolute/project-root/data/processed/retriever/live-cpu-20261024
```

Это явно вызванный live-эксперимент по разрешению пользователя: денежное ограничение
не применяется, каждый вызов записывается до отправки в `reservations.sqlite3`,
запросы/ответы/usage — в `merge.sqlite3`. Финитный `--max-calls` ограничивает один
запрос ретривера, не является тарифом или общей командной квотой. Production CLI
`python -m retriever` сохраняет прежние ограничения общего лимитера. Автоматического
retry/repair нет. Ключ читается только live CLI из окружения или локального `.env`.

Общий индекс переиспользует карточки между компаниями; персональные оценки не
сохраняются в событиях. Существующие каталоги результатов компаний пропускаются
при возобновлении. Для явного повторного отбора сохраните их в отдельной истории:
уже сохранённые API-ответы и векторы будут использованы без повторных вызовов.

Группы, частично выходящие за короткий корпус, отмечаются `incomplete_membership`
и остаются `ambiguous`; они не превращаются в полные кластеры или одиночные новости.

`report.html` — самостоятельный файл без CDN и сетевых запросов: PCA/t-SNE,
синхронные карты до/после, переходы от событий к исходникам, реальные cosine-соседи,
исходная и итоговая выдачи, отложенные операции, полные ответы и структурные ошибки.
PCA обучается только на оригиналах; новые объекты проецируются тем же преобразованием.
t-SNE строится совместно с фиксированным seed; её расстояния не заменяют cosine.
В HTML включены исходные новости и профили: хранить и передавать его как приватные
данные проекта. Внешней публикации команда не выполняет.
