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
consume the downstream analyst batch budget. Existing output files are replaced.
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
