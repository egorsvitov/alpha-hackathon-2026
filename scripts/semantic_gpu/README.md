# GPU semantic experiment

Run from the repository root with a clean Python 3.13 environment. The source
snapshot and all generated artifacts are local and ignored by Git. The default
output is `data/processed/event_pipeline/gpu-experiment`; set
`SEMANTIC_EXPERIMENT_ROOT` to a fresh ignored directory for another experiment.
Output directories are immutable. Preserve prior journals and results.

```bash
uv venv --python 3.13 .venv_events
uv pip install --python .venv_events/bin/python -r llm/events/requirements.lock
uv pip install --python .venv_events/bin/python --torch-backend auto \
  -r llm/event_pipeline/semantic/requirements.txt psutil
export PYTHONPATH=.
export HF_HOME="$PWD/.cache/semantic-bge-m3"
PY=.venv_events/bin/python
EXP=data/processed/event_pipeline/gpu-experiment

# First verify the provided archive checksum and extract its two files.
$PY scripts/semantic_gpu/experiment.py select
$PY -m llm.event_pipeline.semantic plan \
  --corpus data/processed/events/news-corpus-v1 \
  --document-ids "$EXP/selection.json" --output "$EXP/run"
$PY -m llm.event_pipeline.semantic plan \
  --corpus data/processed/events/news-corpus-v1 --output "$EXP/full-run"
$PY scripts/semantic_gpu/freeze_families.py
$PY scripts/semantic_gpu/experiment.py download
$PY scripts/semantic_gpu/experiment.py environment
uv pip freeze --python .venv_events/bin/python > "$EXP/environment.freeze.txt"

# Freeze source-reviewed dev/test pairs and tuning-policy.json BEFORE the sweep.
# Keep every reprint/story family in one partition; unassigned items are exploratory.
# See the delivered local archive for this experiment's exact labels and split.
$PY scripts/semantic_gpu/experiment.py embed --label gpu-smoke --batch-size 2 --max-batches 1
$PY scripts/semantic_gpu/experiment.py embed --label gpu-b16 --batch-size 16 --max-batches 2
$PY scripts/semantic_gpu/experiment.py embed --label gpu-b32 --batch-size 32 --max-batches 2
# Inspect VRAM/RSS, then explicitly select the batch size; do not auto-retry OOM.
$PY scripts/semantic_gpu/experiment.py embed --label sample-complete --batch-size 32
$PY scripts/semantic_gpu/experiment.py embed --label sample-cache-replay --batch-size 32
# Check complete=true; replay must have new_vectors=0.
$PY scripts/semantic_gpu/sweep.py thresholds
$PY scripts/semantic_gpu/sweep.py temporal

# Optional full corpus: import exact text/profile keys, then encode only missing texts.
$PY scripts/semantic_gpu/experiment.py expand-cache
$PY scripts/semantic_gpu/experiment.py embed --run "$EXP/full-run" \
  --label full-complete --batch-size 32
$PY scripts/semantic_gpu/experiment.py embed --run "$EXP/full-run" \
  --label full-cache-replay --batch-size 32
# Compare full corpus using the dev-selected parameters. Set limits explicitly only
# after checking RAM and 40*N*N bytes. This snapshot has >5000 fragments.
OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 $PY scripts/semantic_gpu/sweep.py full
$PY scripts/semantic_gpu/visualization_data.py --comparison "$EXP/comparisons/full-chosen"
```

`experiment.py` records process RSS and device memory every second, allocator
VRAM peaks, model load time, inference time, errors and batch progress. Download
measurements are separate. `download_http.py` is a bounded range-download fallback:
only an exact official LFS size and SHA-256 can enter the pinned model cache.
`MODEL_WEIGHTS_URL` may choose another transfer endpoint; it cannot change the
required hash. The completed local model manifest records every file hash.

The sweep compares three thresholds, then the nine alpha/half-life combinations.
Its criterion uses only explicit dev pairs; absent pair annotations are unknown.
Ties prefer a higher threshold and then proximity to the initial temporal defaults.
It never uses group counts as quality. The held-out story families are small;
keyword rules alone cannot certify that every paraphrase/reprint was found.

`evaluate.py` exports explicit pair coverage and structural group diagnostics.
`review_sample.py` selects 24 diverse groups for a separate source-reading audit:
large groups, singletons with their nearest other document, cross-source groups,
groups changed by time and same-document fragments. Store individual labels,
unit/document IDs, rationale, inspected scope and `single_annotator_draft` in
`review/annotations.json`. Group labels are `useful_together`,
`unrelated_material`, `missing_link`, `uncertain`. The delivered local dev/test
pair labels plus this group audit cover at least 60 cases; they are a Codex draft,
not independent human adjudication. Do not infer quality from a successful summary.

After the local comparison and parameter selection, put the key only in process
`LLM_API_KEY` or `llm/events/.env`, and run:

```bash
$PY -m llm.event_pipeline.semantic.summary \
  --comparison "$EXP/comparisons/CHOSEN_LABEL" \
  --output "$EXP/summaries" --max-groups 80 --target-successes 50 --limit 200
```

The runner uses the project's approved endpoint/model, one call per selected
group and the existing one-shot runtime, lease heartbeat and accounting transport.
A new immutable experiment allowance is explicit in `experiment.json`; the old
journals and B1–B6 limits are never reset or changed. The 200-attempt cap is local
to this experiment, not a statement of the remaining shared 500 RUB/day balance.
Requests, raw responses, provider usage, elapsed time and failures remain in a
persistent quota journal and JSON audit. Unknown RUB cost is `null`. It stops on
transport/HTTP/budget errors. Failed jobs require an explicit retry decision and
are never automatically sent again. A successful rerun reads cached artifacts. It stops after the explicit target of
50 format/reference-valid summaries, or earlier on provider errors; a bounded
reserve of selected groups covers whole-context skips and malformed outputs.
Full-context byte-limit failures skip the whole group without dropping materials.
The selection includes both variants and removes duplicate memberships.
For a documented endpoint/profile change, use a new output directory and
`--journal "$EXP/summaries/quota.sqlite3"` to retain the same consumed allowance.
`--exclude-groups` prevents retries of prior groups; `--groups` accepts frozen,
disjoint worker selections. Concurrent workers share the atomic cap. The delivered
archive records the default-profile failures, a single disabled-thinking capability
probe, and the final compact-prompt worker selections. Use those recorded profiles
and code revisions to reproduce each phase. No automatic profile fallback occurs.

JSON syntax and citation existence are checked separately from semantic support.
A cited fragment belonging to the right document does not prove its claim.
Source content is treated as untrusted data and never rewrites group membership.

```bash
$PY -m unittest discover -s llm/event_pipeline/tests
$PY -m unittest discover -s llm/events/tests
$PY -m llm.event_pipeline check
# Complete REPORT.md, manual annotations and full source/group/summary examples first.
$PY scripts/semantic_gpu/finalize.py --package
```

`finalize.py` validates every comparison hash and complete vector/membership
coverage, aggregates timings/usage and builds a **local, source-bearing archive**
with a SHA-256 manifest. It refuses to package the active API key or `.env` and
excludes working databases/locks. Archive files and the source-bearing report stay
under ignored `data/processed/event_pipeline/`. The archive includes all
`vectors.jsonl`, `units.jsonl`, `documents.jsonl`, memberships and neighbors for a
future interactive visualization. Future 2D distances must not be interpreted as
original cosine distances. Use `environment.freeze.txt` and the exact model
revision when reproducing; the recorded vectors are the exact audit artifact.

## Offline interactive report

The frozen 2026-10-23 experiment can be explored without a GPU, model download or
API access. `build_report.py` reads only JSON/JSONL artifacts. Its curated issue
catalog and conclusions describe this specific snapshot; they are not an
automatic quality assessment of arbitrary future corpora. The source-bearing
`issues.json` is delivered in the local report bundle, outside Git. Retain it
before rebuilding, or pass `--issues PATH/TO/issues.json` for a fresh output.

```bash
# Reuse .venv_events, or create a clean Python 3.13 environment for the report.
uv pip install --python .venv_events/bin/python -r scripts/semantic_gpu/report-requirements.txt
OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4 .venv_events/bin/python \
  scripts/semantic_gpu/build_report.py \
  --experiment data/processed/event_pipeline/gpu-experiment \
  --output data/processed/event_pipeline/interactive-report-20261023
```

Open the resulting `index.html` directly in Chrome. It embeds the data, CSS and
JavaScript and needs neither a server nor a network connection. Its CSP disables
network requests. News, summaries and API request bodies are intentionally local;
do not put generated HTML in Git or on hosting. No credentials, response headers,
working databases or hidden reasoning are exported. The original experiment
archive is not modified. Optional original-artifact links require the sibling
`gpu-experiment` directory, but the interactive UI is self-contained.

`projection.json` caches coordinates by vector SHA-256, seed, parameters and
scikit-learn version. Delete this derived file to recompute; do not modify the
original vectors. PCA50 randomized → Barnes-Hut t-SNE uses perplexity=40,
max_iter=1200, learning_rate=auto, init=pca, seed=20261023 and four CPU threads.
The PCA view uses the first two components of the same PCA fit. Full-corpus k=10
trustworthiness and neighbor recall are measured against original 1024D cosine;
exact duplicate vectors remain and tied ranks use a stable ID order. Hardware/BLAS
differences may alter floating-point coordinates. `manifest.json` records input,
code and output hashes. The semantic/time groupings never depend on projection.

Optional browser integration verification (uses an existing Chrome, no download):

```bash
uv pip install --python .venv_events/bin/python playwright==1.64.1
.venv_events/bin/python scripts/semantic_gpu/check_report.py \
  --chrome /usr/bin/google-chrome
```

Checks include source/date/text filters, empty states, actual canvas selection,
projection and grouping switches, case drilldowns, summary→citation navigation,
all transport/output-limit failures, resource charts, data export, mobile layout,
untrusted-title escaping, zero HTTP requests and zero browser errors. The local
output includes screenshots and `browser-check.json`. No tests send live API calls.
