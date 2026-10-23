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
