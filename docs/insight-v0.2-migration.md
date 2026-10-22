# Insight v0.1 → v0.2

The v0.1 `llm/agent/contracts/insight.schema.json` remains unchanged. Consumers opt into `insight.v0.2.schema.json` by schema_version `0.2`; The B6 consumer accepts `0.1` and `0.2`; these versions are independent of the Insight version.

The envelope now includes event_version, profile_version, generation_mode and generated_at. `relevance` contains the complete pinned ranking Relevance, replacing manually assembled significance. Claims retain input_claim_ids, modality and polarity. Source reports copy one B6 claim exactly in this MVP; AI inference modality/polarity are null and conditionality is expressed by assumptions/support_status.

Every section has a state and reason. Empty risk/opportunity arrays do not establish absence of risk/opportunity. Unknowns have stable IDs, affected_paths and clarification questions. Fixture output is always limited, with independent semantic review explicitly not_run and usage.pricing_status=no_llm_calls.

Source IDs changed: news IDs use `news:` plus SHA-256 of canonical JSON `[event_id,event_version,evidence_id]`; bank IDs use `bank:` plus SHA-256 of `[document_sha256,chunk_id,char_start,char_end,text_sha256]`. Original evidence/article IDs and versions remain in registry records. Quote text hashes use SHA-256 of UTF-8 quote bytes. The code selects sources/features by validated references; generator output cannot replace quotes, URLs, profile values or hashes. Registry results are independent deep copies. Bank fragments additionally require corpus verification before entering context; synthetic bank material remains explicitly synthetic and is never published as an established bank view by the offline QA.

Trace records actual bounded workflow steps, artifacts, QA axes and versions. Local RunStore artifacts preserve pinned inputs for restart and follow-up child runs. The example `data/examples/personalization/agent/insight.v0.2.json` comes from the deterministic synthetic route and demonstrates the contract only. `forecast-draft.json` plus `forecast-context.json` demonstrates source-exact forecast reporting and conditional contract checks with unknown inputs; it is not a model or real financial analysis.

Live provider calls remain unavailable until the shared budget/provider gateway in 06A is implemented. The ports define that integration boundary; no keys are read by this implementation.
