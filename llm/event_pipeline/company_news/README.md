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
