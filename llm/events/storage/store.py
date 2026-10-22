"""B2: immutable article archive and durable job state; no executor or LLM calls."""

from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import TypeAdapter, ValidationError, JsonValue

from llm.events.contracts.models import ArticleInput, ArticleRecord, Issue
from llm.events.storage.models import ArtifactRecord, AttemptRecord, BatchCounts, BatchItem, BatchReceipt, JobRecord, ProcessingSpec, RestoreResult, VersionInfo
from llm.events.storage.schema import APPLICATION_ID, DDL, SCHEMA_VERSION, TABLES
from llm.events.contracts.validation import ContractValidationError, article_sha256

from llm.events.paths import ROOT
DEFAULT_DB = ROOT / "data/processed/events/state.sqlite3"
LEASE_SECONDS = 900
JSON_VALUE = TypeAdapter(JsonValue)


class StorageError(ContractValidationError):
    def __init__(self, code, message, path="/", *, issues=None):
        self.code = code
        super().__init__(issues or [Issue(code=code, path=path, message=message)])


def canonical_json(value):
    try:
        JSON_VALUE.validate_python(value, strict=True)
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        result.encode("utf-8")  # Reject lone Unicode surrogates before binding SQLite text.
        return result
    except (ValidationError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise StorageError("invalid_json_payload", "Payload must be a finite, JSON-compatible value") from exc


def processing_hash(spec: ProcessingSpec) -> str:
    return hashlib.sha256(canonical_json(spec.model_dump(mode="json")).encode("utf-8")).hexdigest()


def source_id_for(article_id: str, version: int) -> str:
    return str(uuid5(NAMESPACE_URL, canonical_json(["alpha-events", article_id, version])))


def _stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch is not None else None


def _model(model, payload):
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise StorageError("invalid_argument", "Invalid input", issues=[
            Issue(code="invalid_type_or_value", path="/" + "/".join(map(str, e["loc"])), message=e["msg"])
            for e in exc.errors()
        ]) from exc


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise StorageError("invalid_argument", f"{name} must be a non-empty string", f"/{name}")
    return value


def _positive(value, name):
    if type(value) is not int or value <= 0:
        raise StorageError("invalid_argument", f"{name} must be a positive integer", f"/{name}")
    return value


def _reject_constant(value):
    raise ValueError(f"Non-standard JSON constant: {value}")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


class EventStore:
    """Short-lived connections, explicit transactions, one local SQLite file.

    clock is a callable returning UTC epoch seconds; injectable for lease tests.
    busy_timeout_ms defaults to 5000; tests may use 0 to avoid waiting.
    """

    def __init__(self, db_path=None, *, clock=time.time, busy_timeout_ms=5000):
        if str(db_path) == ":memory:":
            raise StorageError("invalid_database_path", "A persistent SQLite file is required")
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
            raise StorageError("invalid_argument", "busy_timeout_ms must be a nonnegative integer")
        self.path = Path(db_path) if db_path is not None else DEFAULT_DB
        self.clock = clock
        self.busy_timeout_ms = busy_timeout_ms
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError("storage_io_error", str(exc)) from exc
        self._initialize()

    def _open(self):
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=self.busy_timeout_ms / 1000)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            return conn
        except BaseException:
            conn.close()
            raise

    @staticmethod
    def _sqlite_error(exc):
        code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return StorageError("storage_busy", "SQLite is busy; retry this operation later")
        if isinstance(exc, sqlite3.IntegrityError):
            return StorageError("storage_integrity_error", str(exc))
        return StorageError("storage_error", str(exc))

    @contextmanager
    def _transaction(self, *, write=False):
        conn = None
        try:
            conn = self._open()
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if (conn.execute("PRAGMA application_id").fetchone()[0], conn.execute("PRAGMA user_version").fetchone()[0]) != (APPLICATION_ID, SCHEMA_VERSION):
                raise StorageError("unsupported_storage_schema", "Unexpected application ID or schema version")
            yield conn
            conn.commit()
        except BaseException as exc:
            if conn is not None:
                conn.rollback()
            if isinstance(exc, sqlite3.Error):
                raise self._sqlite_error(exc) from exc
            raise
        finally:
            if conn is not None:
                conn.close()

    def _initialize(self):
        conn = None
        try:
            conn = self._open()
            conn.execute("BEGIN IMMEDIATE")
            app = conn.execute("PRAGMA application_id").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if app == 0 and version == 0 and not tables:
                for statement in DDL:
                    conn.execute(statement)
                conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif app == APPLICATION_ID and version in (1, 2, 3):
                raise StorageError("storage_migration_required", "Database requires explicit migration: python -m llm.events.publication.cli --db <path> migrate")
            elif (app, version) != (APPLICATION_ID, SCHEMA_VERSION) or not TABLES.issubset(tables) or (version == SCHEMA_VERSION and conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='catalog_article_invalidated'").fetchone() is None):
                raise StorageError("unsupported_storage_schema", "Database is foreign, incomplete or has an unsupported schema; it was not recreated")
            conn.commit()
            conn.execute("PRAGMA journal_mode=WAL")
        except BaseException as exc:
            if conn is not None:
                conn.rollback()
            if isinstance(exc, sqlite3.Error):
                raise self._sqlite_error(exc) from exc
            raise
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _record(row):
        return ArticleRecord(article=ArticleInput.model_validate(json.loads(row["payload_json"])),
                             article_version=row["article_version"], source_id=row["source_id"])

    @staticmethod
    def _require(row, kind, identifier):
        if row is None:
            raise StorageError(f"{kind}_not_found", str(identifier))
        return row

    @staticmethod
    def _head(conn, article_id):
        return conn.execute("SELECT v.* FROM article_heads h JOIN article_versions v ON v.source_id=h.current_source_id WHERE h.article_id=?", (article_id,)).fetchone()

    @staticmethod
    def _is_current(conn, source_id):
        return conn.execute("SELECT 1 FROM article_heads WHERE current_source_id=?", (source_id,)).fetchone() is not None

    def _supersede(self, conn, article_id, now):
        jobs = conn.execute("""SELECT j.* FROM jobs j JOIN article_versions v ON j.source_id=v.source_id
            JOIN article_heads h ON h.article_id=v.article_id
            WHERE v.article_id=? AND j.source_id<>h.current_source_id
            AND j.state IN ('pending','running','deferred')""", (article_id,)).fetchall()
        reason = "Article has a newer current revision"
        issues = canonical_json([Issue(code="article_superseded", path="/source_id", message=reason).model_dump()])
        for job in jobs:
            if job["active_attempt_id"]:
                conn.execute("UPDATE attempts SET status='superseded',finished_at=?,issues_json=? WHERE attempt_id=?", (now, issues, job["active_attempt_id"]))
            conn.execute("UPDATE jobs SET state='superseded',active_attempt_id=NULL,updated_at=?,reason=? WHERE job_id=?", (now, reason, job["job_id"]))

    def _create_version(self, conn, article, now, *, restored_from=None, reason=None):
        version = conn.execute("SELECT COALESCE(MAX(article_version),0)+1 FROM article_versions WHERE article_id=?", (article.article_id,)).fetchone()[0]
        sid = source_id_for(article.article_id, version)
        conn.execute("""INSERT INTO article_versions
            (source_id,article_id,article_version,article_hash,payload_json,received_at,restored_from_source_id,restoration_reason)
            VALUES (?,?,?,?,?,?,?,?)""", (sid, article.article_id, version, article_sha256(article),
                canonical_json(article.model_dump(mode="json")), now, restored_from, reason))
        conn.execute("""INSERT INTO article_heads(article_id,current_source_id) VALUES (?,?)
            ON CONFLICT(article_id) DO UPDATE SET current_source_id=excluded.current_source_id""", (article.article_id, sid))
        self._supersede(conn, article.article_id, now)
        return ArticleRecord(article=article, article_version=version, source_id=sid)

    @staticmethod
    def _prepare(raw_bytes):
        try:
            payload = json.loads(raw_bytes.decode("utf-8-sig"), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise StorageError("invalid_batch_json", str(exc)) from exc
        if not isinstance(payload, list):
            raise StorageError("invalid_batch_root", "Expected a JSON array")
        prepared, identities = [], defaultdict(set)
        for i, raw in enumerate(payload):
            aid = raw.get("article_id") if isinstance(raw, dict) else None
            aid = aid if isinstance(aid, str) and aid else None
            try:
                article = ArticleInput.model_validate(raw)
                errors = []
                fingerprint = "valid:" + article_sha256(article)
            except ValidationError as exc:
                article = None
                errors = [Issue(code="invalid_type_or_value", path=f"/items/{i}/" + "/".join(map(str, e["loc"])), message=e["msg"]) for e in exc.errors()]
                fingerprint = "invalid:" + json.dumps(raw, ensure_ascii=True, sort_keys=True)
            except UnicodeError:
                article = None
                errors = [Issue(code="invalid_unicode", path=f"/items/{i}", message="Article contains an unpaired Unicode surrogate")]
                fingerprint = "invalid:" + json.dumps(raw, ensure_ascii=True, sort_keys=True)
            if aid:
                try:
                    aid.encode("utf-8")
                except UnicodeError:
                    aid = None
            if aid:
                identities[aid].add(fingerprint)
            prepared.append((aid, article, errors))
        for i, (aid, article, errors) in enumerate(prepared):
            if aid and len(identities[aid]) > 1:
                prepared[i] = (aid, None, [Issue(code="ambiguous_article_versions", path=f"/items/{i}", message="Different variants of this article_id occur in one batch")])
        return prepared

    def import_batch(self, source: bytes | str | Path, *, source_name=None) -> BatchReceipt:
        """Accept exact file bytes or a path; receipt is created before parsing."""
        if isinstance(source, bytes):
            raw = source
        else:
            try:
                raw = Path(source).read_bytes()
            except (OSError, TypeError) as exc:
                raise StorageError("input_io_error", str(exc)) from exc
            if source_name is None:
                source_name = Path(source).name
        if source_name is not None and not isinstance(source_name, str):
            raise StorageError("invalid_argument", "source_name must be a string or null")
        if source_name is not None:
            try:
                source_name.encode("utf-8")
            except UnicodeError as exc:
                raise StorageError("invalid_argument", "source_name must contain valid Unicode") from exc
        batch_id = str(uuid4())
        with self._transaction(write=True) as conn:
            conn.execute("""INSERT INTO batches(batch_id,source_name,received_at,raw_sha256,raw_bytes,import_status,issues_json)
                VALUES (?,?,?,?,?,'importing','[]')""", (batch_id, source_name, self.clock(), hashlib.sha256(raw).hexdigest(), raw))
        return self.resume_batch(batch_id)

    def _import_item(self, batch_id, index, aid, article, errors):
        """One transaction per element; a crash cannot leave an unreceipted revision."""
        with self._transaction(write=True) as conn:
            if conn.execute("SELECT 1 FROM batch_items WHERE batch_id=? AND item_index=?", (batch_id, index)).fetchone():
                return
            if errors:
                sid, previous, disposition = None, None, "invalid"
            else:
                head = self._head(conn, article.article_id)
                previous = head["source_id"] if head else None
                digest = article_sha256(article)
                if head and head["article_hash"] == digest:
                    sid, disposition = head["source_id"], "unchanged"
                else:
                    old = conn.execute("SELECT source_id FROM article_versions WHERE article_id=? AND article_hash=? ORDER BY article_version DESC LIMIT 1", (article.article_id, digest)).fetchone()
                    if old:
                        sid, disposition = old["source_id"], "historical"
                    else:
                        record = self._create_version(conn, article, self.clock())
                        sid, disposition = record.source_id, "updated" if head else "new"
            conn.execute("""INSERT INTO batch_items(batch_id,item_index,article_id,source_id,previous_source_id,disposition,issues_json)
                VALUES (?,?,?,?,?,?,?)""", (batch_id, index, aid, sid, previous, disposition,
                                          canonical_json([e.model_dump() for e in errors])))

    def resume_batch(self, batch_id: str) -> BatchReceipt:
        with self._transaction() as conn:
            batch = self._require(conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone(), "batch", batch_id)
            raw, status = batch["raw_bytes"], batch["import_status"]
        if status != "importing":
            return self.get_batch(batch_id)
        try:
            prepared = self._prepare(raw)
        except StorageError as exc:
            with self._transaction(write=True) as conn:
                conn.execute("UPDATE batches SET import_status='rejected',finished_at=?,issues_json=? WHERE batch_id=? AND import_status='importing'", (self.clock(), canonical_json([e.model_dump() for e in exc.issues]), batch_id))
            return self.get_batch(batch_id)
        with self._transaction(write=True) as conn:
            conn.execute("UPDATE batches SET total_count=? WHERE batch_id=? AND import_status='importing'", (len(prepared), batch_id))
        for i, (aid, article, errors) in enumerate(prepared):
            self._import_item(batch_id, i, aid, article, errors)
        with self._transaction(write=True) as conn:
            invalid = conn.execute("SELECT COUNT(*) FROM batch_items WHERE batch_id=? AND disposition='invalid'", (batch_id,)).fetchone()[0]
            status = "rejected" if prepared and invalid == len(prepared) else "partial" if invalid else "completed"
            conn.execute("UPDATE batches SET import_status=?,finished_at=? WHERE batch_id=? AND import_status='importing'", (status, self.clock(), batch_id))
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> BatchReceipt:
        with self._transaction() as conn:
            batch = self._require(conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone(), "batch", batch_id)
            saved = {r["item_index"]: r for r in conn.execute("SELECT * FROM batch_items WHERE batch_id=? ORDER BY item_index", (batch_id,))}
            items, changes, counts = [], [], BatchCounts()
            for i in range(batch["total_count"] or 0):
                row = saved.get(i)
                item = BatchItem(index=i, article_id=row["article_id"] if row else None,
                    source_id=row["source_id"] if row else None, previous_source_id=row["previous_source_id"] if row else None,
                    disposition=row["disposition"] if row else "pending",
                    issues=json.loads(row["issues_json"]) if row else [])
                setattr(counts, item.disposition, getattr(counts, item.disposition) + 1)
                items.append(item)
                if item.disposition in {"new", "updated"}:
                    changes.append({"article_id": item.article_id, "previous_source_id": item.previous_source_id, "source_id": item.source_id})
            return BatchReceipt(batch_id=batch_id, source_name=batch["source_name"], received_at=_stamp(batch["received_at"]),
                finished_at=_stamp(batch["finished_at"]), raw_sha256=batch["raw_sha256"], import_status=batch["import_status"],
                total_count=batch["total_count"], counts=counts, items=items, issues=json.loads(batch["issues_json"]), current_changes=changes)

    def get_batch_bytes(self, batch_id: str) -> bytes:
        with self._transaction() as conn:
            return self._require(conn.execute("SELECT raw_bytes FROM batches WHERE batch_id=?", (batch_id,)).fetchone(), "batch", batch_id)["raw_bytes"]

    def list_batches(self, *, import_status=None) -> list[BatchReceipt]:
        """Discover receipts, particularly imports interrupted before returning an ID."""
        if import_status not in {None, "importing", "completed", "partial", "rejected"}:
            raise StorageError("invalid_argument", "Unknown import_status")
        with self._transaction() as conn:
            rows = conn.execute("SELECT batch_id FROM batches WHERE (? IS NULL OR import_status=?) ORDER BY received_at,batch_id", (import_status, import_status)).fetchall()
        return [self.get_batch(r["batch_id"]) for r in rows]

    def get_article(self, source_id: str) -> ArticleRecord:
        with self._transaction() as conn:
            return self._record(self._require(conn.execute("SELECT * FROM article_versions WHERE source_id=?", (source_id,)).fetchone(), "article", source_id))

    def get_current_article(self, article_id: str) -> ArticleRecord:
        with self._transaction() as conn:
            return self._record(self._require(self._head(conn, article_id), "article", article_id))

    def list_versions(self, article_id: str) -> list[VersionInfo]:
        with self._transaction() as conn:
            head = self._require(self._head(conn, article_id), "article", article_id)
            rows = conn.execute("SELECT * FROM article_versions WHERE article_id=? ORDER BY article_version", (article_id,)).fetchall()
            return [VersionInfo(record=self._record(r), article_sha256=r["article_hash"], received_at=_stamp(r["received_at"]),
                is_current=r["source_id"] == head["source_id"], restored_from_source_id=r["restored_from_source_id"], restoration_reason=r["restoration_reason"]) for r in rows]

    def restore_version(self, article_id: str, article_version: int, reason: str) -> RestoreResult:
        _positive(article_version, "article_version")
        _text(reason, "reason")
        with self._transaction(write=True) as conn:
            head = self._require(self._head(conn, article_id), "article", article_id)
            old = self._require(conn.execute("SELECT * FROM article_versions WHERE article_id=? AND article_version=?", (article_id, article_version)).fetchone(), "article_version", article_version)
            if old["source_id"] == head["source_id"]:
                raise StorageError("already_current", "This version is already current")
            record = self._create_version(conn, self._record(old).article, self.clock(), restored_from=old["source_id"], reason=reason)
            return RestoreResult(record=record, previous_source_id=head["source_id"], restored_from_source_id=old["source_id"], reason=reason)

    @staticmethod
    def _spec(payload):
        spec = _model(ProcessingSpec, payload)
        canonical_json(spec.model_dump(mode="json"))
        return spec

    @staticmethod
    def _attempt_record(row):
        return AttemptRecord(attempt_id=row["attempt_id"], job_id=row["job_id"],
            attempt_number=row["attempt_number"], token=row["token"], worker_id=row["worker_id"],
            status=row["status"], started_at=_stamp(row["started_at"]),
            lease_expires_at=_stamp(row["lease_expires_at"]), finished_at=_stamp(row["finished_at"]),
            issues=json.loads(row["issues_json"]))

    def _job_record(self, conn, job_id):
        row = self._require(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone(), "job", job_id)
        attempt = conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (row["active_attempt_id"],)).fetchone() if row["active_attempt_id"] else None
        return JobRecord(job_id=job_id, source_id=row["source_id"], spec_hash=row["spec_hash"],
            spec=ProcessingSpec.model_validate(json.loads(row["spec_json"])), state=row["state"],
            created_at=_stamp(row["created_at"]), updated_at=_stamp(row["updated_at"]),
            is_current=self._is_current(conn, row["source_id"]), reason=row["reason"],
            result_artifact_id=row["result_artifact_id"], active_attempt=self._attempt_record(attempt) if attempt else None)

    def get_job(self, job_id: str) -> JobRecord:
        with self._transaction() as conn:
            return self._job_record(conn, job_id)

    def list_attempts(self, job_id: str) -> list[AttemptRecord]:
        with self._transaction() as conn:
            self._require(conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone(), "job", job_id)
            return [self._attempt_record(r) for r in conn.execute("SELECT * FROM attempts WHERE job_id=? ORDER BY attempt_number", (job_id,))]

    def enqueue(self, source_id: str, spec: ProcessingSpec) -> JobRecord:
        spec = self._spec(spec)
        digest = processing_hash(spec)
        with self._transaction(write=True) as conn:
            self._require(conn.execute("SELECT 1 FROM article_versions WHERE source_id=?", (source_id,)).fetchone(), "article", source_id)
            if not self._is_current(conn, source_id):
                raise StorageError("article_not_current", "Only the current article revision can be queued")
            row = conn.execute("SELECT job_id FROM jobs WHERE source_id=? AND spec_hash=?", (source_id, digest)).fetchone()
            if row:
                return self._job_record(conn, row["job_id"])
            job_id, now = str(uuid4()), self.clock()
            conn.execute("""INSERT INTO jobs(job_id,source_id,spec_hash,spec_json,state,created_at,updated_at)
                VALUES (?,?,?,?,'pending',?,?)""", (job_id, source_id, digest, canonical_json(spec.model_dump(mode="json")), now, now))
            return self._job_record(conn, job_id)

    def claim_next(self, spec: ProcessingSpec, worker_id: str, *, lease_seconds=LEASE_SECONDS, source_id=None) -> JobRecord | None:
        spec = self._spec(spec)
        _text(worker_id, "worker_id")
        _positive(lease_seconds, "lease_seconds")
        with self._transaction(write=True) as conn:
            row = conn.execute("""SELECT j.* FROM jobs j JOIN article_heads h ON h.current_source_id=j.source_id
                WHERE j.spec_hash=? AND j.state='pending' AND (? IS NULL OR j.source_id=?)
                ORDER BY j.created_at,j.job_id LIMIT 1""", (processing_hash(spec),source_id,source_id)).fetchone()
            if row is None:
                return None
            job_id, now = row["job_id"], self.clock()
            attempt_id, token = str(uuid4()), str(uuid4())
            number = conn.execute("SELECT COALESCE(MAX(attempt_number),0)+1 FROM attempts WHERE job_id=?", (job_id,)).fetchone()[0]
            conn.execute("""INSERT INTO attempts(attempt_id,job_id,attempt_number,token,worker_id,status,started_at,lease_expires_at,issues_json)
                VALUES (?,?,?,?,?,'running',?,?,'[]')""", (attempt_id, job_id, number, token, worker_id, now, now + lease_seconds))
            conn.execute("UPDATE jobs SET state='running',active_attempt_id=?,updated_at=?,reason=NULL WHERE job_id=?", (attempt_id, now, job_id))
            return self._job_record(conn, job_id)

    def _active_attempt(self, conn, token, now):
        row = conn.execute("""SELECT a.*,j.state AS job_state,j.active_attempt_id,j.source_id FROM attempts a
            JOIN jobs j ON j.job_id=a.job_id WHERE a.token=?""", (token,)).fetchone()
        if row is None:
            raise StorageError("attempt_not_found", "Unknown attempt token")
        if (row["status"] != "running" or row["job_state"] != "running" or row["active_attempt_id"] != row["attempt_id"]
                or row["lease_expires_at"] <= now or not self._is_current(conn, row["source_id"])):
            raise StorageError("stale_attempt", "Attempt is expired, completed or belongs to a superseded article")
        return row

    def renew_lease(self, token: str, *, lease_seconds=LEASE_SECONDS) -> AttemptRecord:
        _positive(lease_seconds, "lease_seconds")
        with self._transaction(write=True) as conn:
            now = self.clock()
            attempt = self._active_attempt(conn, token, now)
            expires = max(attempt["lease_expires_at"], now + lease_seconds)
            conn.execute("UPDATE attempts SET lease_expires_at=? WHERE attempt_id=?", (expires, attempt["attempt_id"]))
            conn.execute("UPDATE jobs SET updated_at=? WHERE job_id=?", (now, attempt["job_id"]))
            return self._attempt_record(conn.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt["attempt_id"],)).fetchone())

    @staticmethod
    def _artifact_record(row):
        return ArtifactRecord(artifact_id=row["artifact_id"], attempt_id=row["attempt_id"], kind=row["kind"],
            schema_version=row["schema_version"], payload=json.loads(row["payload_json"]), created_at=_stamp(row["created_at"]))

    @staticmethod
    def _insert_artifact(conn, attempt_id, kind, payload_json, schema_version, now):
        artifact_id = str(uuid4())
        conn.execute("INSERT INTO artifacts(artifact_id,attempt_id,kind,schema_version,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                     (artifact_id, attempt_id, kind, schema_version, payload_json, now))
        return artifact_id

    def save_artifact(self, token: str, kind: str, payload, *, schema_version=None) -> ArtifactRecord:
        _text(kind, "kind")
        if schema_version is not None:
            _text(schema_version, "schema_version")
        serialized = canonical_json(payload)
        with self._transaction(write=True) as conn:
            now = self.clock()
            attempt = self._active_attempt(conn, token, now)
            aid = self._insert_artifact(conn, attempt["attempt_id"], kind, serialized, schema_version, now)
            return self._artifact_record(conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone())

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        with self._transaction() as conn:
            row = self._require(conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)).fetchone(), "artifact", artifact_id)
            return self._artifact_record(row)

    def list_artifacts(self, attempt_id: str) -> list[ArtifactRecord]:
        with self._transaction() as conn:
            self._require(conn.execute("SELECT 1 FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone(), "attempt", attempt_id)
            return [self._artifact_record(r) for r in conn.execute("SELECT * FROM artifacts WHERE attempt_id=? ORDER BY created_at,artifact_id", (attempt_id,))]

    def complete_attempt(self, token: str, payload, *, kind="result", schema_version=None) -> JobRecord:
        if payload is None:
            raise StorageError("missing_result", "A successful job must have a non-null result; use an explicit empty list/object for zero findings")
        _text(kind, "kind")
        if schema_version is not None:
            _text(schema_version, "schema_version")
        serialized = canonical_json(payload)
        with self._transaction(write=True) as conn:
            now = self.clock()
            attempt = self._active_attempt(conn, token, now)
            aid = self._insert_artifact(conn, attempt["attempt_id"], kind, serialized, schema_version, now)
            conn.execute("UPDATE attempts SET status='succeeded',finished_at=? WHERE attempt_id=?", (now, attempt["attempt_id"]))
            conn.execute("UPDATE jobs SET state='succeeded',active_attempt_id=NULL,result_artifact_id=?,updated_at=?,reason=NULL WHERE job_id=?", (aid, now, attempt["job_id"]))
            return self._job_record(conn, attempt["job_id"])

    def fail_attempt(self, token: str, issues: list[Issue]) -> JobRecord:
        if not isinstance(issues, list) or not issues:
            raise StorageError("invalid_argument", "Provide at least one structured error")
        issues = [_model(Issue, issue) for issue in issues]
        with self._transaction(write=True) as conn:
            now = self.clock()
            attempt = self._active_attempt(conn, token, now)
            conn.execute("UPDATE attempts SET status='failed',finished_at=?,issues_json=? WHERE attempt_id=?", (now, canonical_json([e.model_dump() for e in issues]), attempt["attempt_id"]))
            conn.execute("UPDATE jobs SET state='failed',active_attempt_id=NULL,updated_at=?,reason=? WHERE job_id=?", (now, issues[0].message, attempt["job_id"]))
            return self._job_record(conn, attempt["job_id"])

    def defer_job(self, job_id: str, reason: str, *, token=None) -> JobRecord:
        _text(reason, "reason")
        with self._transaction(write=True) as conn:
            row = self._require(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone(), "job", job_id)
            if not self._is_current(conn, row["source_id"]):
                raise StorageError("article_not_current", "Cannot defer a historical job")
            now = self.clock()
            if row["state"] == "running":
                attempt = self._active_attempt(conn, token, now)
                if attempt["job_id"] != job_id:
                    raise StorageError("wrong_attempt", "Token belongs to another job")
                issue = Issue(code="job_deferred", path="/", message=reason)
                conn.execute("UPDATE attempts SET status='deferred',finished_at=?,issues_json=? WHERE attempt_id=?", (now, canonical_json([issue.model_dump()]), attempt["attempt_id"]))
            elif row["state"] != "pending" or token is not None:
                raise StorageError("invalid_job_state", "Only pending jobs or a valid active attempt can be deferred")
            conn.execute("UPDATE jobs SET state='deferred',active_attempt_id=NULL,updated_at=?,reason=? WHERE job_id=?", (now, reason, job_id))
            return self._job_record(conn, job_id)

    def retry_job(self, job_id: str) -> JobRecord:
        with self._transaction(write=True) as conn:
            row = self._require(conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone(), "job", job_id)
            if not self._is_current(conn, row["source_id"]):
                raise StorageError("article_not_current", "Cannot retry a historical revision")
            if row["state"] not in {"failed", "deferred"}:
                raise StorageError("invalid_job_state", "Only failed/deferred jobs can be retried explicitly")
            conn.execute("UPDATE jobs SET state='pending',updated_at=?,reason=NULL WHERE job_id=?", (self.clock(), job_id))
            return self._job_record(conn, job_id)

    def requeue_expired(self) -> list[JobRecord]:
        with self._transaction(write=True) as conn:
            now = self.clock()
            attempts = conn.execute("""SELECT a.*,j.source_id FROM attempts a JOIN jobs j ON j.active_attempt_id=a.attempt_id
                WHERE j.state='running' AND a.status='running' AND a.lease_expires_at<=?""", (now,)).fetchall()
            result = []
            for attempt in attempts:
                current = self._is_current(conn, attempt["source_id"])
                reason = "Attempt lease expired"
                issues = canonical_json([Issue(code="lease_expired", path="/lease_expires_at", message=reason).model_dump()])
                conn.execute("UPDATE attempts SET status=?,finished_at=?,issues_json=? WHERE attempt_id=?", ("interrupted" if current else "superseded", now, issues, attempt["attempt_id"]))
                conn.execute("UPDATE jobs SET state=?,active_attempt_id=NULL,updated_at=?,reason=? WHERE job_id=?", ("pending" if current else "superseded", now, reason, attempt["job_id"]))
                result.append(self._job_record(conn, attempt["job_id"]))
            return result

    def get_cached_result(self, source_id: str, spec: ProcessingSpec) -> ArtifactRecord | None:
        """Exact successful revision+spec result, including explicitly requested history."""
        spec = self._spec(spec)
        with self._transaction() as conn:
            self._require(conn.execute("SELECT 1 FROM article_versions WHERE source_id=?", (source_id,)).fetchone(), "article", source_id)
            row = conn.execute("""SELECT a.* FROM jobs j JOIN artifacts a ON a.artifact_id=j.result_artifact_id
                WHERE j.source_id=? AND j.spec_hash=? AND j.state='succeeded'""", (source_id, processing_hash(spec))).fetchone()
            return self._artifact_record(row) if row else None
