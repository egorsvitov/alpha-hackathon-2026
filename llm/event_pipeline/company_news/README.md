# Company → raw news: shared CPU/GPU experiment

Contract owner: `news-pipeline-gpu-lazy-retrieval`, branched from `6648a15`.
This directory owns the experimental runner and schemas. Search/index/encoder/
reranker adapters belong in `retriever/`, preserving its existing B6 path.

The first integration commit contains shared schemas and one synthetic fixture.
CPU and GPU implementations must consume the same `fixtures/shared-input.v1.json`.
Do not fork these contracts; coordinate changes as separate commits.

```bash
PYTHONPATH=. .venv_events/bin/python -m llm.event_pipeline.company_news.contract_fixtures
.venv_events/bin/python -m unittest discover -s llm/event_pipeline/company_news/tests
```

The versioned entrypoints are `RawNewsCandidateBatch` (`raw-news-candidate-batch.v1`),
`NewsAnalysisInput` (`raw-news-analysis.v1`) and `ExperimentManifest`
(`company-news-experiment.v1`). `NewsDocumentRef` preserves source/snapshot identity
and date precision; its `content_hash` is SHA-256 of **unmodified UTF-8 body text**,
not embedding text. `article_version` may carry the prepared-news article hash.
Offsets are Python Unicode indices into that body. `SourceSpan.resolve` verifies
both bounds and the source hash. No EventBundle or SourceClaim is fabricated.

`query_id` is SHA-256 of canonical JSON of all other CompanyQuery fields.
Serialization uses UTF-8, sorted object keys, compact separators and finite
numbers. Query family is separate from view (`business/finance/trade_geo`).
Mechanisms are conditional and require known feature anchors; hypotheses must
retain `status=hypothesis`. Unknown company values remain explicit unknowns.
Query deduplication for inference must preserve all provenance bindings.

Candidates retain hits from every query/channel and a separate exclusion stage/
reason. Retrieval/reranker scores are not probabilities. Context expansion has
its own role and parent, never inherits a retrieved document's score. Missing
publication dates remain unknown. As-of eligibility and length limits are policies
recorded by the runner, not invented metadata.

Schemas reject extra fields, stale company/profile references, mismatched source
hashes, invalid spans, unknown query references and hypothesis-status loss.
Source-bearing artifacts belong in ignored `data/processed/company_news/`.
Live analysis is a separate explicit command requiring an existing shared journal,
max-calls and a budget. This integration does not reset prior quotas.

## Reproduce the delivered run

Observed environment: Python 3.13.5, RTX 5070 Ti Laptop 12 GB, CUDA torch
2.14.1+cu132. `requirements-observed.lock` captures the actual environment, not
the older colleague's CUDA130 lock. Restore CUDA132 wheels from the matching
PyTorch index when recreating; CPU-only installs cannot run index/rerank.
Install `rfc3339-validator` as locked: otherwise jsonschema silently lacks its
date-time format check. Browser inspection additionally used Playwright and the
installed Google Chrome. No browser/CDN assets are needed to read the HTML.

Private inputs must be restored locally: prepared-news.v1, company CSV, validated
activity catalogue and the previous full semantic run/comparison (including their
manifests). The plan verifies article/body hashes, comparison output hashes,
snapshot/run identities and exact original text. Existing semantic setup and
download instructions: `scripts/semantic_gpu/README.md`. Never publish these inputs.

```bash
export PYTHONPATH=.
export HF_HOME="$PWD/.cache/semantic-bge-m3"
PY=.venv_events/bin/python
RUN=data/processed/company_news/reproduction-20261023

# Explicit network preparation; immutable official revisions and hashes.
$PY -m llm.event_pipeline.company_news.download_model
$PY -m llm.event_pipeline.company_news.download_model --e5

$PY -m llm.event_pipeline.company_news --root "$RUN" plan \
  --corpus data/processed/events/news-corpus-v1 \
  --companies data/companies/df_Uid_data_hack.csv \
  --catalogue data/processed/retriever/activity-hypotheses-v1/catalogue/catalogue.json
HF_HUB_OFFLINE=1 $PY -m llm.event_pipeline.company_news --root "$RUN" index --batch-size 8
OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 \
  $PY -m llm.event_pipeline.company_news --root "$RUN" retrieve
$PY -m llm.event_pipeline.company_news --root "$RUN" review-pool --split dev
```

For a new run, review the full originals and company features, write
`judgments-dev.jsonl`, and run `evaluate --split dev`. Keep unknown/uncertain
separate from reject. For exact replay, restore the delivered source-hashed
judgments and frozen story split from the private archive. The delivered labels
are manual source reviews by the agent, **not independent human gold**. The
missing CPU control344 and shared CPU judgments are not fabricated.

```bash
$PY -m llm.event_pipeline.company_news --root "$RUN" evaluate --split dev
# Delivered dev choice G2. A new experiment must choose using its own dev only.
HF_HUB_OFFLINE=1 $PY -m llm.event_pipeline.company_news --root "$RUN" rerank --base-variant G2
$PY -m llm.event_pipeline.company_news --root "$RUN" select --reserve 0
$PY -m llm.event_pipeline.company_news --root "$RUN" evaluate --split dev
# Delivered reserve choice 4; ablations R0/R4/R8 are retained in every selection.
$PY -m llm.event_pipeline.company_news --root "$RUN" select --reserve 4
$PY -m llm.event_pipeline.company_news --root "$RUN" review-pool --split test
# Freeze test labels; do not tune after looking at this result.
$PY -m llm.event_pipeline.company_news --root "$RUN" evaluate --split test
$PY -m llm.event_pipeline.company_news --root "$RUN" analyze
$PY -m llm.event_pipeline.company_news --root "$RUN" report
```

`plan` is immutable and refuses an existing manifest. Every stage checks frozen
plan hashes; dependent stages check artifact hashes. Successful index replay is
a verified no-op. Partial vectors and reranker pairs persist in SQLite caches.
The delivered E5 baseline was cold inference in fixed document order; a verified
completed index replays without inference. The shared cache also offers a tested
incremental CachedEncoder wrapper; default B6 dimension stays384.
Exact text/query dedup retains every company/query provenance binding. OOM stops
and writes diagnostics; restarting rerank is explicit and resumes cached pairs.
Do not silently change model/batch/precision/length policy in a failed run.

Variants: C0 BM25; C1 CPU E5; C2 hybrid; D0 dense BGE; G0 BGE+BM25;
G1 +anchored mechanisms; G2 +catalogue hypotheses; G3 G2+reranker with dev-selected
reserve4; G4 +exact duplicate suppression/group expansion within spare budget.
A_fusion collapses families to the existing retriever's channel-max RRF control;
A_freshness changes only publication-age weighting after eligibility. No graph
challenger was run because historical graph availability is unproven.
Search is exact CPU cosine in batches16×512 over GPU-produced BGE vectors;
128-company timings measure this search alone, not end-to-end retrieval.
The additional request benchmark includes BM25, cosine and fusion per company
with frozen query vectors, separately from model loading/inference/reranking:

```bash
OPENBLAS_NUM_THREADS=2 OMP_NUM_THREADS=2 \
  $PY -m llm.event_pipeline.company_news.benchmark --root "$RUN"
```

R@20/50/100 in G3/G4/R* refer to the raw reranker ordering before the reserve;
final metrics evaluate the actual reserved byte-budget selection. Macro recall
averages only companies with at least one known positive. Test has just one.

## Lazy analysis and optional live execution

`analyze` validates bounded selected sources through
`backend/app/analyst/raw_adapter.py` and prepares requests only. It sends nothing.
Common card keys include exact references/spans/text/title, prompt/config and the
entire provider profile. Personal keys also include company/profile version,
as_of, analytical profile, query provenance and exact common-card keys.
Preview remains the original quotes. No LLM output or quality is fabricated.

Legacy analyst modules/fixtures were imported from ef84f53, plus the required
schema/orchestrator dependencies; existing `batch_adapter.py` is unchanged.
Legacy transport retains its old behavior, but **raw execution does not use its
retry/repair gateway**: it injects the existing audited OneShotRunner transport.
The existing 276 non-API analyst tests, 47 retriever tests (one opt-in skip),
36 ranking tests and 40 pipeline tests pass. New tests cover raw source identity,
budgets, query dedup/cache restart, conflicting numbers/negations, rerank reserve,
unscored length overflow and cross-company common-card reuse.

Live execution is separate and fail-closed. Supply an existing finite shared
request counter, existing shared EventStore monetary journal, a verified
ProviderProfile JSON and confirmed ExecutionLimits JSON. The handoff allocates
no new quota. Do not create replacement journals. `max-calls` is a durable
experiment cap in the shared store, including failed reservations; opening a new
output directory cannot reset it. Full request byte counts and labelled bytes/4
estimates precede sending; conservative reservations use verified profile bounds.
Unknown costs/reservations stay accounted. Raw responses/usage/errors are saved
before parsing. No retries, repair calls, model or endpoint fallback occur.

```bash
# Rebuild planned keys with the same verified profile before any live call.
$PY -m llm.event_pipeline.company_news --root "$RUN" analyze --model-profile /private/verified-profile.json
$PY -m llm.event_pipeline.company_news --root "$RUN" analyze-live \
  --journal /private/existing-request-counter.sqlite3 \
  --shared-store /private/existing-shared-monetary-store.sqlite3 \
  --profile-path /private/verified-profile.json \
  --budget-path /private/confirmed-execution-limits.json --max-calls 6 --variant G4
```

The number6 is syntax illustration, not authorization. Actual journal/cap/budget
remain user-supplied. Credentials come only from the process or local
`llm/events/.env`; never put them in config/artifacts. The raw adapter checks
reference bounds/hash, source selection and Russian language; semantic support
remains `unreviewed`. A failure leaves original news readable.

Local HTML: `data/processed/company_news/run-20261023/index.html`. It embeds all
3012 originals once and provides company/variant/family selectors, interactive
PCA, stage exclusions, query paths, groups, card plans, manual judgments and an
issue register. Public export `docs/reports/company-news-gpu-20261023/` contains
only aggregates and the source-free report. Archive private JSONL/NPY/HTML and
checksums, not keys, `.env`, model weights, vector caches or working databases.

```bash
$PY -m llm.event_pipeline.company_news.package --root "$RUN" \
  --output data/processed/company_news/handoff/company-news-gpu-20261023.tar.gz
```

The private archive has `ARTIFACTS.sha256` for every payload file and an external
SHA-256 for the archive itself. Source-bearing HTML should never be staged.
