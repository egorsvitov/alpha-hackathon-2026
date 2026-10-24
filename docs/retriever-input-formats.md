# Standalone retriever inputs

Imports perform no encoding, NLP, truncation, or timezone guessing. All examples
should use synthetic data; source articles and vector matrices remain local.

`read_articles(path)` accepts a JSON array, `{ "articles": [...] }`, or JSONL.
Each row is either the parser's seven fields (`article_id`, `source`, `url`,
`title`, `published_at`, `language`, `text`), a complete `ArticleRevision`, or a
prepared wrapper with `article` (also accepted as `raw`) containing those fields.
The wrapper may provide `document_id`/`revision_id`, `text_sha256`,
`observed_at`, `title_origin`, `publication_precision`, and a list of provenance
objects. Prepared provenance may supply a unique `title_origin` and
`publication: {precision: 'day', date: 'YYYY-MM-DD', raw: '...'}` when the article
has no publication timestamp. Conflicting origins or dates are rejected. Wrapper
metadata is preserved in provenance. An opaque legacy `article_sha256` is retained
as metadata; its undocumented canonicalization is not guessed. The top-level
exact `text_sha256`, when present, is always checked. Explicit immutable IDs
are preserved; absent IDs become `article:` plus a canonical digest of the
input row, including metadata. Conflicting duplicate revision IDs are errors;
identical duplicates receive diagnostics. Empty or whitespace-only texts receive
`empty_text` diagnostics and are excluded. Long texts remain complete.

A date `YYYY-MM-DD` has precision `day`; timestamps must have an explicit UTC
offset. Null dates have precision `unknown`. Explicit unknown dates retain the
original value in provenance. No received-at value is fabricated.

`read_clusters(path, articles)` accepts JSON arrays, `{ "clusters": [...] }`,
or JSONL of complete `ClusterSnapshot` objects. Members must be sorted, unique,
and identify imported revision IDs. The membership hash is checked when present.
Overlapping groups all become ambiguous, with a diagnostic for every shared
revision. No group is chosen arbitrarily.

`read_interests(path)` accepts a complete `CompanyInterestSet` JSON object.
Interest vectors must be unit normalized and have the declared dimension.
Inactive interests and, by default, interests with family `graph` are excluded
from search. `include_graph=True` enables active graph interests.

## Native vector bundle v1

`read_vector_bundle(path, articles)` accepts a manifest JSON path or a directory
containing `manifest.json`. Both inline JSON vectors and NPY matrices share:

```json
{
  "schema_version": "retriever-vector-bundle.v1",
  "corpus_id": "synthetic-v1",
  "corpus_sha256": "<canonical full corpus digest>",
  "article_sources": {"revision-a": "<sha256 of exact source text>"},
  "encoder_fingerprint": "<exact pinned encoder and text policy fingerprint>",
  "dimension": 2,
  "items": ["<complete SearchItem objects>"],
  "vectors": ["<complete VectorRecord objects>"]
}
```

Generate `corpus_sha256` with `retriever.importers.corpus_sha256(articles)`.
It hashes complete revisions sorted by `revision_id`, including provenance,
metadata, and body. `article_sources` must exactly match all supplied revisions
and their exact text hashes. Items must reference only those revisions, and all
revisions must be represented by at least one item. Article items must bind exactly one source revision, with identical title and
body. Their embedding text must equal that body, or the source title followed by
two newlines and the body when `title_origin` is `source`. Every item requires vectors;
duplicate item/vector IDs and mismatched item versions are errors.

Inline `vectors` are `VectorRecord` objects with `vector_id`, `item_id`,
`item_version`, `embedding_text_sha256`, and `values`. Exact text hashes use
SHA256 over UTF-8 bytes. Vectors must be finite, unit normalized within 1e-5,
and match `dimension`. No implicit normalization or encoder substitution occurs.
The consuming snapshot separately enforces the exact encoder fingerprint.

For NPY, replace `vectors` with these four fields:

```json
{
  "matrix_file": "vectors.npy",
  "matrix_sha256": "<sha256 of complete file bytes>",
  "mapping_file": "mapping.jsonl",
  "mapping_sha256": "<sha256 of complete file bytes>"
}
```

The matrix must be a two-dimensional floating-point NPY array with one row per
mapping entry and the declared dimension; pickle loading is disabled. Mapping
is a JSON array or JSONL, in matrix row order. Each row has the VectorRecord
fields except `values`. File paths must resolve inside the bundle directory.
Both artifact hashes are mandatory and checked before loading.

Several rows may point to the same item, representing fragments. An NPY mapping
row supplies `embedding_text` for its exact fragment; inline manifests supply
`fragment_texts: { "vector-id": "exact fragment" }`. Each fragment must be a
nonempty exact contiguous slice of one of that item's source article bodies,
and its hash must match `embedding_text_sha256`. Events require full-text vectors.
The importer computes `item.derived_from.fragment_text_hashes` from the validated
fragments, replacing any supplied values, and stores proof text in
`item.derived_from.fragment_texts` as a mapping from fragment hash to exact slice.
Both proof fields are validated again by the store and search. Otherwise vector hashes must match the
complete `item.embedding_text`.

Search computes every interest's maximum over an item's fragments, then the
maximum across participating interests. Results include all per-interest scores
and sort by descending score followed by `item_id`. Equal interest scores choose
the lexicographically smallest `interest_id`. Snapshots contain active items only;
there is no persisted company score or search-time activation inference.

Legacy experiment artifacts are not autodetected. Convert them explicitly into
this manifest, preserving their original IDs, exact source hashes and encoder
fingerprint. Merely matching dimensions is insufficient, and the retriever never
loads old NLP pipelines or generates replacement vectors.
