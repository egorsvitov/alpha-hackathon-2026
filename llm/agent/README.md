> После удаления старого ранкера `analyze` требует явный `--relevance <file.json>` в fixture-режиме. Автоматического rank_events больше нет. Ниже описана существующая аналитическая инфраструктура; новый вход ImpactCandidateBatch ещё требует [адаптации R7](../../docs/retriever/implementation-plan.md).

# Offline interpretation workflow

`analyze_event(snapshot, profile, relevance, *, role, as_of, services)` validates the pinned inputs, retrieves evidence, packages bounded context, generates a draft, checks QA and saves an Insight v0.2. `answer_question(...)` uses immutable child runs and the original pinned context. `ports.py` owns shared request/result/QA/Services types. QA and history contracts are documented in `docs/implementation/05c-result.md`.

Only explicit `mode=fixture` works. The generator uses synthetic templates for source copies and conditional loan/deposit contract checks. It demonstrates code paths, not financial reasoning quality. Fixture output is always limited; semantic review is not_run. Live access is closed in code, regardless of configuration flags, key presence or budget object. No credentials are read. The v0.1 schema is retained; see `docs/insight-v0.2-migration.md`.

Run from the repository root:

```bash
uv run --frozen python -m llm.agent analyze --event docs/b6-examples/forecast.json --profiles data/examples/personalization/profiles.json --company-id DEMO_TRADE --role cfo --as-of 2026-10-20T18:00:00+03:00 --mode fixture --state-dir data/processed/personalization/agent-demo
uv run --frozen python -m llm.agent ask --run-id RUN_ID --company-id DEMO_TRADE --role cfo --question 'Каких данных не хватает для оценки влияния на кредит?' --mode fixture --state-dir data/processed/personalization/agent-demo
uv run --frozen python -m unittest discover -s llm/agent/tests -v
```

CLI uses the shared B6 ingestion path, ranking rules and synthetic `llm/rag/tests/fixtures/corpus.jsonl`. An explicit `--corpus` selects another local corpus. Paths may be absolute or relative; state directories contain event/run SQLite files and immutable artifacts, so use a writable persistent volume. JSON goes to stdout or `--output`; diagnostics go to stderr. Sources and profiles are never modified.

Mandatory context contains identity/time, complete source claims and all conflicting variants, selected profile features including unknown/conflicting quality, ranking reasons and limitations. Bank hits exceeding max_bank_hits/max_quote_chars or max_context_bytes are excluded whole, in deterministic order, before stopping for context_overflow. Defaults: 131072 bytes, 5 bank hits, 6000 quote characters. These are offline byte bounds, not live token bounds. No selected source fact or conflict is truncated.

Workflow limits: up to three retrieval attempts (including the initial attempt), two generation attempts total, zero semantic reviews in this implementation. A retrieval requiring regeneration consumes the same generation cap. Failed QA after exhaustion produces insufficient_evidence without claims, never ready. Source/user text is untrusted data. There are no generic agent frameworks or model-controlled Python tool dispatches.

## Preprocessor compatibility after news-pipeline update

The local ranking/agent route accepts EventBundle v0.1 and v0.2 through `ranking.b6`, preserving scenario/value_role and pinned payload hashes. B6 publication/export is implemented upstream. JSON timestamps include seconds and timezone. Scenario inferences remain conditional; numerical before/after/comparison inferences require semantic review. See [consumer migration](../../docs/b6-v02-consumer-migration.md).
