# 01A + 01B: contracts and ingestion

Исходный commit: `2453da0e72fe8e78a302391527edaf278d046cb2`, ветка `news-personalization`.

Оба подэтапа реализованы. Внешние B6 schema/четыре fixtures сохранены без изменений.
CompanyProfile, envelope company-profiles, Relevance и RankingResult v0.1 находятся
в `docs/contracts/`; полностью вымышленные минимальные примеры —
`data/examples/personalization/contracts/`. Insight/EvidenceBundle принадлежат 04/05.

## Интерфейсы

- `ranking.contracts.ValidationIssue(code, path, message, severity='error')`;
  безопасные `ContractError`/`InputError`/`ContractValidationError`/
  `BundleValidationError`/`EventVersionCollision`/`EventNotFound` с `.code/.issues`.
- `canonical_json`, `payload_hash` (`canonical_hash` alias), `parse_timestamp`,
  RFC6901 `resolve_pointer`, `validate_contract(value, schema_name)` и
  `require_valid_contract`. Draft202012Validator + FormatChecker кэшируются.
  Semantic checks проверяют ссылки/значения features, provenance даты снимка,
  graph counters/fractions и score/contributions/diagnostics/feed subset.
- `b6.decode_json`, `load_bundles(path, max_bytes=32MiB, max_line_bytes=4MiB)`;
  strict UTF-8, duplicate keys/nonfinite constants запрещены. JSONL decode errors
  содержат 1-based номер строки. `validate_bundle` возвращает issues;
  `require_valid_bundle` бросает BundleValidationError только для severity=error.
- `SnapshotRef(event_id,event_version,payload_sha256,received_at,source_mode,ingest_id)`;
  StoredEvent дополнительно `.bundle/.validation_issues`, `.snapshot` и `.to_dict()`.
  Время в Python — aware datetime, JSON — UTC ISO timestamp.
- `EventStore(path)` поддерживает context manager, `put(bundle, *, received_at,
  source_mode)`, `get_latest(event_id, as_of)`, `list_latest(as_of)`, `close()`.
  `ingest_bundles(bundles, *, store, clock, source_mode)` получает одно время batch,
  валидирует всё и пишет одной SQLite transaction (включая rollback collision).
  `Clock` Protocol, `FixtureClock(timestamp)` и `SystemClock` в contracts.
  IngestResult `.status/.snapshot/.validation_issues`, `.issues` alias;
  `.to_dict()` возвращает контрактные status/snapshot/issues.
- `ingest_id` детерминирован из mode, event key, canonical hash, first received_at;
  unchanged сохраняет первые metadata. INTEGER versions сортируются численно;
  срез ограничивает received_at и producer updated_at, затем выбирает max version,
  включая withdrawn. Future publication хранится; downstream применяет feed policy.

## CLI

`python -m ranking validate-b6 --input <json|jsonl>` — JSON отчёт, ненулевой exit при ошибке.
`python -m ranking ingest --input <json|jsonl> --state-dir <dir> --mode fixture
--fixture-clock 2026-10-20T11:00:00+03:00` — JSON inserted/unchanged/snapshot/issues.
Fixture использует отдельный `fixture-events.sqlite3`, live — `events.sqlite3`.
Fixture clock в live запрещён. Относительный state-dir разрешается от project root.
Ошибки ingest/аргументов — JSON в stderr; валидатор выводит JSON report в stdout.
Общий CLI вызывает `profile_cli.register_parser(subparsers)` и
`rank_cli.register_parser(subparsers)`, затем `args.func(args)`;
отсутствующий модуль hook не блокирует validate/ingest, внутренние import errors не скрываются.

## Проверки

- `uv lock`, `uv sync --frozen` — успешно; Python 3.13.5, 9 locked packages.
- `uv run --frozen python -m unittest ranking.tests.test_b6 ranking.tests.test_event_store ranking.tests.test_contracts -v`
  — 20 тестов успешно.
- Все четыре B6 fixtures проходят schema + semantic validation;
  text quote equality честно отмечена `original_text_not_checked` warning.
- Negative/mutation cases: source/evidence refs, Decimal range, dates, duplicate IDs,
  quote spans/emoji, pointer escapes/array indices, structural coverage, source flags,
  closed topics/open factors, integer version types, empty active/withdrawn claims.
- Store: atomic batch validation/collision rollback, warning persistence, idempotence,
  receipt and producer cutoffs, numeric 9→10→2, v2→v1→withdrawn v3→active v4,
  unknown IDs, persistence after reopen, concurrent put, one clock call,
  deterministic replay across DBs. Tests read no raw CSV/PDF or network.
- `uv run --frozen python -m unittest discover -s ranking/tests -q` —
  последний общий прогон: 52 tests, OK (включая profiles/graph/signals/engine/integration
  на текущем общем workspace; следующие изменения других агентов независимы).
- Фактически выполнены validate-b6 normal и два fixture CLI ingest по команде выше:
  valid=true, затем inserted/unchanged; first receipt и ingest_id совпадают.

## Файлы и ограничения

Созданы pyproject.toml/uv.lock, ranking/__init__.py/contracts.py/b6.py/event_store.py/
__main__.py, три test modules, четыре core schemas и четыре synthetic fixtures.
Обновлены .gitignore и THIRD_PARTY_NOTICES с сохранением прежних изменений.
Производные ranking/personalization, SQLite sidecars и local package build artifacts
игнорируются. Raw материалы не изменены и не скопированы в fixtures.

`no_llm_calls`, стоимость 0. Live B6 producer, API/UI integration, semantic truth/
quote support QA, original article lookup — not_run/external to this task.
Source-flag contradictions дают warning, когда B6 не запрещает комбинацию явно;
warning сохраняется вне payload. SQLite предназначен для локального общего тома.
Artifact ID/hash графа проверяются структурно; локальное разрешение и содержимое
артефакта принадлежат graph repository, а не generic JSON schema validator.
