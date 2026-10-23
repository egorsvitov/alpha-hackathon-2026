# Raw-news CPU/GPU integration checkpoint

23 October 2026. GPU branch: `news-pipeline-gpu-lazy-retrieval`, parent `6648a15`.
The pre-existing context file named in the supplied Telegram handoff was absent
from this checkout and the inspected remote branches; this file records the
implementation agreement derived from that handoff, not a recovered upstream file.

Shared contracts/fixtures are owned by this GPU branch under
`llm/event_pipeline/company_news/`. First transfer: schemas, synthetic fixture,
validators and `retriever/raw_ports.py`. CPU work should consume that commit.
Common-file updates should be transferred as small reviewed commits.

Verified dependencies: `feature/impact-retriever` f7ea837 and `feature/analyst`
ef84f53. Their full implementations are absent from the parent checkout. Import
only reviewed, pinned modules; do not merge all historic ranking/agent changes.
Preserve existing B6 paths and the previous semantic experiment.

Available prepared-news snapshot: 3621 documents, 5128 semantic units, 5035 unique
representations. The short-publication subset and exact representation reuse must
be measured, not assumed. Old E5 company vectors cannot be compared to BGE news.
Local profiles and corpora must remain outside Git.

No live quota is allocated by this plan. Pending user input: location of any newer
CPU integration context, plus existing journal, max-calls and budget for live
end-to-end analysis. Offline/GPU work proceeds independently.

## Delivered GPU implementation

The branch now imports reviewed retriever/profile modules from f7ea837 and analyst
modules plus their synthetic regression dependencies from ef84f53. No wholesale
feature-branch merge or API mode switch was made. The older B6 adapter is retained.

The local frozen experiment is `data/processed/company_news/run-20261023/`:
3012 short originals, 609 excluded long bodies, 20 diagnostic profiles, 128 separate
performance profiles. BGE reuses 2955 unique exact-text vectors, with independent
8-document parity verification; E5 covers2994 with18 explicit token exclusions.
Catalogue provenance and shared semantic snapshot identities are verified.

All runner commands are implemented under `llm/event_pipeline/company_news/`.
New raw adapters live under `retriever/raw_*` and `backend/app/analyst/raw_adapter.py`.
Shared fixture budget names are now `final_documents` and `context_utf8_bytes`;
the raw analyst rejects incomplete budgets before transport. Contract schema
versions remain v1; the first-transfer fixture was synthetic throughout.

Dev selected G2 and reserve4; final test parameters were frozen before test
evaluation. Manual agent source review covers248 pairs across20 profiles, not
independent human gold. Test has only one send; superiority is not established.
G4 did not save common-card calls (133 unique planned cards vs122 for G3).

The public aggregate report is `docs/reports/company-news-gpu-20261023/report.md`.
Full source-bearing interactive HTML, matrices, query paths, judgments and card
plans remain local. No new live calls were made. Existing journal/cap/budget,
control344, shared CPU judgments and original integration context remain missing.
The raw single-attempt path has a synthetic transport test proving durable cap1,
one provider invocation and cache-only replay; it is not a live quality result.
