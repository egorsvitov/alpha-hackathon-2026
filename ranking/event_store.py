"""SQLite snapshot store. Batch ingestion is atomic, including collisions."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
from .b6 import require_valid_bundle
from .contracts import (StoredEvent, SnapshotRef, IngestResult, ValidationIssue, EventNotFound,
                        EventVersionCollision, parse_timestamp, canonical_json, payload_hash,
                        FixtureClock, SystemClock)

class EventStore:
    def __init__(self, path):
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS events (
            event_id TEXT NOT NULL, event_version INTEGER NOT NULL,
            payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
            received_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            source_mode TEXT NOT NULL, ingest_id TEXT NOT NULL, validation_issues TEXT NOT NULL,
            PRIMARY KEY(event_id,event_version))""")
        self.connection.commit()
    def close(self):
        self.connection.close()
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.close()
    @staticmethod
    def _stored(row):
        return StoredEvent(row["event_id"],row["event_version"],row["payload_sha256"],parse_timestamp(row["received_at"]),row["source_mode"],row["ingest_id"],json.loads(row["payload"]),tuple(ValidationIssue(**v) for v in json.loads(row["validation_issues"])))
    def _put_validated(self, bundle, *, received_at, source_mode, issues):
        event = bundle["event"]
        digest = payload_hash(bundle)
        row = self.connection.execute("SELECT * FROM events WHERE event_id=? AND event_version=?",(event["event_id"],event["event_version"])).fetchone()
        if row:
            if row["payload_sha256"] != digest:
                raise EventVersionCollision(issues=[ValidationIssue("event_version_collision", "/event/event_version", "Existing event version has a different payload")])
            stored = self._stored(row)
            return IngestResult("unchanged",stored.snapshot,stored.validation_issues)
        stamp = parse_timestamp(received_at).isoformat()
        ingest_id = payload_hash({"event_id":event["event_id"],"event_version":event["event_version"],"payload_sha256":digest,"received_at":stamp,"source_mode":source_mode})
        self.connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",(event["event_id"],event["event_version"],canonical_json(bundle),digest,stamp,parse_timestamp(event["updated_at"]).isoformat(),source_mode,ingest_id,canonical_json([x.to_dict() for x in issues])))
        return IngestResult("inserted",SnapshotRef(event["event_id"],event["event_version"],digest,parse_timestamp(stamp),source_mode,ingest_id),tuple(issues))
    def put(self, bundle, *, received_at, source_mode):
        return self._batch([bundle], received_at=received_at, source_mode=source_mode)[0]
    def _batch(self, bundles, *, received_at, source_mode):
        if source_mode not in {"live","fixture"}:
            raise ValueError("source_mode must be live or fixture")
        received_at = parse_timestamp(received_at)
        bundles = list(bundles)
        reports = [require_valid_bundle(b) for b in bundles]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            result = [self._put_validated(b, received_at=received_at,source_mode=source_mode,issues=issues) for b,issues in zip(bundles,reports)]
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise
    def get_latest(self, event_id, as_of):
        stamp = parse_timestamp(as_of).isoformat()
        row = self.connection.execute("SELECT * FROM events WHERE event_id=? AND received_at<=? AND updated_at<=? ORDER BY event_version DESC LIMIT 1",(event_id,stamp,stamp)).fetchone()
        if row is None:
            raise EventNotFound(issues=[ValidationIssue("event_not_found", "/event/event_id", "No snapshot is known on this cutoff")])
        return self._stored(row)
    def list_latest(self, as_of):
        stamp = parse_timestamp(as_of).isoformat()
        rows = self.connection.execute("""SELECT e.* FROM events e JOIN (
            SELECT event_id,MAX(event_version) AS version FROM events WHERE received_at<=? AND updated_at<=? GROUP BY event_id
            ) latest ON e.event_id=latest.event_id AND e.event_version=latest.version ORDER BY e.event_id""",(stamp,stamp)).fetchall()
        return [self._stored(row) for row in rows]

def ingest_bundles(bundles, *, store, clock, source_mode):
    """Assign one receiver timestamp, validate all, then commit all or none."""
    if source_mode == "live" and isinstance(clock, FixtureClock):
        raise ValueError("FixtureClock is only permitted in fixture mode")
    received_at = parse_timestamp(clock.now())
    return store._batch(bundles, received_at=received_at, source_mode=source_mode)
