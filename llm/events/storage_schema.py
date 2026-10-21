"""SQLite schema v4, retaining the v1 DDL for explicit migrations and tests."""

APPLICATION_ID = 0x414C5048
SCHEMA_VERSION = 4
TABLES = {"article_versions", "article_heads", "batches", "batch_items", "jobs", "attempts", "artifacts"}

DDL = [
    """CREATE TABLE article_versions (
        source_id TEXT PRIMARY KEY,
        article_id TEXT NOT NULL,
        article_version INTEGER NOT NULL CHECK(article_version > 0),
        article_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        received_at REAL NOT NULL,
        restored_from_source_id TEXT REFERENCES article_versions(source_id),
        restoration_reason TEXT,
        UNIQUE(article_id, article_version),
        CHECK((restored_from_source_id IS NULL) = (restoration_reason IS NULL))
    )""",
    "CREATE INDEX article_history ON article_versions(article_id, article_hash, article_version)",
    """CREATE TABLE article_heads (
        article_id TEXT PRIMARY KEY,
        current_source_id TEXT NOT NULL UNIQUE REFERENCES article_versions(source_id)
    )""",
    """CREATE TABLE batches (
        batch_id TEXT PRIMARY KEY,
        source_name TEXT,
        received_at REAL NOT NULL,
        finished_at REAL,
        raw_sha256 TEXT NOT NULL,
        raw_bytes BLOB NOT NULL,
        import_status TEXT NOT NULL CHECK(import_status IN ('importing','completed','partial','rejected')),
        total_count INTEGER CHECK(total_count >= 0),
        issues_json TEXT NOT NULL
    )""",
    """CREATE TABLE batch_items (
        batch_id TEXT NOT NULL REFERENCES batches(batch_id),
        item_index INTEGER NOT NULL CHECK(item_index >= 0),
        article_id TEXT,
        source_id TEXT REFERENCES article_versions(source_id),
        previous_source_id TEXT REFERENCES article_versions(source_id),
        disposition TEXT NOT NULL CHECK(disposition IN ('new','updated','unchanged','historical','invalid')),
        issues_json TEXT NOT NULL,
        PRIMARY KEY(batch_id,item_index)
    )""",
    """CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES article_versions(source_id),
        spec_hash TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed','deferred','superseded')),
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        reason TEXT,
        active_attempt_id TEXT REFERENCES attempts(attempt_id),
        result_artifact_id TEXT REFERENCES artifacts(artifact_id),
        UNIQUE(source_id,spec_hash),
        CHECK((state='running') = (active_attempt_id IS NOT NULL)),
        CHECK((state='succeeded') = (result_artifact_id IS NOT NULL))
    )""",
    "CREATE INDEX jobs_pending ON jobs(spec_hash,state,created_at,job_id)",
    """CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES jobs(job_id),
        attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
        token TEXT NOT NULL UNIQUE,
        worker_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','deferred','interrupted','superseded')),
        started_at REAL NOT NULL,
        lease_expires_at REAL NOT NULL,
        finished_at REAL,
        issues_json TEXT NOT NULL,
        UNIQUE(job_id,attempt_number),
        CHECK((status='running') = (finished_at IS NULL))
    )""",
    "CREATE INDEX attempts_expiring ON attempts(status,lease_expires_at)",
    """CREATE TABLE artifacts (
        artifact_id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
        kind TEXT NOT NULL,
        schema_version TEXT,
        payload_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE TRIGGER batch_input_immutable BEFORE UPDATE OF raw_bytes,raw_sha256,received_at,source_name ON batches
        BEGIN SELECT RAISE(ABORT,'batch input is immutable'); END""",
]

for table in ("article_versions", "batch_items", "artifacts"):
    for action in ("UPDATE", "DELETE"):
        DDL.append(f"""CREATE TRIGGER {table}_{action.lower()}_immutable BEFORE {action} ON {table}
            BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END""")

# Keep the exact B2 DDL available for explicit migration regression tests.
B2_DDL = tuple(DDL)
B2_TABLES = frozenset(TABLES)
B3_DDL = [
    """CREATE TABLE budget_days (
        budget_day TEXT PRIMARY KEY,
        quota_rub TEXT NOT NULL,
        blocked INTEGER NOT NULL DEFAULT 0 CHECK(blocked IN (0,1)),
        block_reason TEXT
    )""",
    """CREATE TABLE llm_calls (
        call_id TEXT PRIMARY KEY,
        attempt_id TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
        budget_day TEXT NOT NULL REFERENCES budget_days(budget_day),
        created_at REAL NOT NULL,
        finished_at REAL,
        request_json TEXT NOT NULL,
        tariffs_json TEXT NOT NULL,
        reserved_rub TEXT NOT NULL,
        accounted_rub TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved','settled','unknown','reconciled')),
        response_json TEXT,
        error_json TEXT,
        reconciliation_reason TEXT
    )""",
    "CREATE INDEX calls_day ON llm_calls(budget_day,created_at)",
    """CREATE TRIGGER call_identity_immutable BEFORE UPDATE OF attempt_id,budget_day,created_at,request_json,tariffs_json,reserved_rub ON llm_calls
        BEGIN SELECT RAISE(ABORT,'call identity and reservation are immutable'); END""",
    """CREATE TRIGGER call_delete_immutable BEFORE DELETE ON llm_calls
        BEGIN SELECT RAISE(ABORT,'call audit cannot be deleted'); END""",
    """CREATE TRIGGER call_response_immutable BEFORE UPDATE OF response_json,error_json,finished_at ON llm_calls
        WHEN OLD.finished_at IS NOT NULL
        BEGIN SELECT RAISE(ABORT,'call response is immutable'); END""",
]
DDL.extend(B3_DDL)
TABLES = TABLES | {'budget_days','llm_calls'}

# Freeze the complete v2 schema independently of the current target version.
V2_DDL = tuple(DDL)
V2_TABLES = frozenset(TABLES)
B5_DDL = [
    """CREATE TABLE catalogs (
        catalog_id TEXT PRIMARY KEY, config_json TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 0,
        stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0,1)),
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE catalog_inputs (
        catalog_id TEXT NOT NULL REFERENCES catalogs(catalog_id),
        article_id TEXT NOT NULL, source_id TEXT NOT NULL REFERENCES article_versions(source_id),
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
        PRIMARY KEY(catalog_id,article_id)
    )""",
    """CREATE TABLE catalog_runs (
        run_id TEXT PRIMARY KEY, catalog_id TEXT NOT NULL REFERENCES catalogs(catalog_id),
        revision INTEGER NOT NULL, fingerprint TEXT NOT NULL, created_at REAL NOT NULL,
        manifest_json TEXT NOT NULL, decisions_json TEXT NOT NULL,
        UNIQUE(catalog_id,revision)
    )""",
    """CREATE TABLE catalog_events (
        event_id TEXT PRIMARY KEY, catalog_id TEXT NOT NULL REFERENCES catalogs(catalog_id),
        created_order INTEGER NOT NULL, anchor_claim_id TEXT NOT NULL, identity_json TEXT NOT NULL
    )""",
    """CREATE TABLE catalog_event_versions (
        event_id TEXT NOT NULL REFERENCES catalog_events(event_id),
        event_version INTEGER NOT NULL, payload_json TEXT NOT NULL,
        PRIMARY KEY(event_id,event_version)
    )""",
    """CREATE TABLE catalog_run_events (
        run_id TEXT NOT NULL REFERENCES catalog_runs(run_id),
        event_id TEXT NOT NULL, event_version INTEGER NOT NULL,
        PRIMARY KEY(run_id,event_id),
        FOREIGN KEY(event_id,event_version) REFERENCES catalog_event_versions(event_id,event_version)
    )""",
    """CREATE TABLE catalog_memberships (
        run_id TEXT NOT NULL REFERENCES catalog_runs(run_id), claim_id TEXT NOT NULL,
        event_id TEXT NOT NULL REFERENCES catalog_events(event_id),
        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
        PRIMARY KEY(run_id,claim_id)
    )""",
    """CREATE TABLE catalog_reviews (
        review_id TEXT PRIMARY KEY, catalog_id TEXT NOT NULL REFERENCES catalogs(catalog_id),
        created_order INTEGER NOT NULL, created_at REAL NOT NULL,
        left_id TEXT NOT NULL, right_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        revoked_review_id TEXT REFERENCES catalog_reviews(review_id)
    )""",
    "CREATE INDEX catalog_input_sources ON catalog_inputs(source_id)",
    """CREATE TRIGGER catalog_article_invalidated AFTER UPDATE OF current_source_id ON article_heads
        WHEN OLD.current_source_id<>NEW.current_source_id
        BEGIN UPDATE catalogs SET stale=1,generation=generation+1 WHERE catalog_id IN
        (SELECT catalog_id FROM catalog_inputs WHERE article_id=NEW.article_id); END""",
]
for table in ('catalog_runs','catalog_events','catalog_event_versions','catalog_run_events','catalog_memberships','catalog_reviews'):
    for action in ('UPDATE','DELETE'):
        B5_DDL.append(f"""CREATE TRIGGER {table}_{action.lower()}_immutable BEFORE {action} ON {table}
            BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END""")
DDL.extend(B5_DDL)
TABLES |= {'catalogs','catalog_inputs','catalog_runs','catalog_events','catalog_event_versions',
           'catalog_run_events','catalog_memberships','catalog_reviews'}

# Frozen v3 remains the source for explicit B6 migration.
V3_DDL = tuple(DDL)
V3_TABLES = frozenset(TABLES)
B6_DDL = [
    """CREATE TABLE bundle_publications (
        publication_id TEXT PRIMARY KEY,
        catalog_id TEXT NOT NULL REFERENCES catalogs(catalog_id),
        sequence INTEGER NOT NULL CHECK(sequence > 0),
        run_id TEXT NOT NULL REFERENCES catalog_runs(run_id),
        catalog_generation INTEGER NOT NULL,
        rules_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(catalog_id, sequence)
    )""",
    """CREATE TABLE bundle_versions (
        event_id TEXT NOT NULL REFERENCES catalog_events(event_id),
        event_version INTEGER NOT NULL CHECK(event_version > 0),
        payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
        PRIMARY KEY(event_id, event_version)
    )""",
    """CREATE TABLE bundle_publication_events (
        publication_id TEXT NOT NULL REFERENCES bundle_publications(publication_id),
        event_id TEXT NOT NULL, event_version INTEGER NOT NULL,
        catalog_event_version INTEGER NOT NULL,
        PRIMARY KEY(publication_id, event_id),
        FOREIGN KEY(event_id, event_version) REFERENCES bundle_versions(event_id, event_version),
        FOREIGN KEY(event_id, catalog_event_version) REFERENCES catalog_event_versions(event_id, event_version)
    )""",
]
for table in ('bundle_publications', 'bundle_versions', 'bundle_publication_events'):
    for action in ('UPDATE', 'DELETE'):
        B6_DDL.append(f"""CREATE TRIGGER {table}_{action.lower()}_immutable BEFORE {action} ON {table}
            BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END""")
DDL.extend(B6_DDL)
TABLES |= {'bundle_publications', 'bundle_versions', 'bundle_publication_events'}
