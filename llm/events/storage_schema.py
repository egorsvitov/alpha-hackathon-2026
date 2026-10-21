"""SQLite state schema v1. No implicit migrations or external SQL dependencies."""

APPLICATION_ID = 0x414C5048
SCHEMA_VERSION = 1
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
