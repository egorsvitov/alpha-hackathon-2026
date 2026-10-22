"""Append-only SQLite run history with content-addressed JSON artifacts.

Run identifiers are SQL values, never filesystem paths. A finished run is sealed;
follow-up work always gets a new run with parent_run_id.
"""
from __future__ import annotations
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from ranking.contracts import canonical_json


def _jsonable(value):
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timezone-aware timestamp required")
        return value.isoformat()
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("JSON object keys must be strings")
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _encode(value):
    return canonical_json(_jsonable(value))


def _decode(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result
    return json.loads(value, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


class RunStore:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript('''
          CREATE TABLE IF NOT EXISTS agent_runs (
            run_id TEXT PRIMARY KEY, parent_run_id TEXT REFERENCES agent_runs(run_id),
            context TEXT NOT NULL, context_sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS agent_artifacts (
            artifact_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS agent_run_artifacts (
            run_id TEXT NOT NULL REFERENCES agent_runs(run_id), ordinal INTEGER NOT NULL,
            kind TEXT NOT NULL, artifact_id TEXT NOT NULL REFERENCES agent_artifacts(artifact_id),
            PRIMARY KEY(run_id,ordinal));
          CREATE TABLE IF NOT EXISTS agent_steps (
            run_id TEXT NOT NULL REFERENCES agent_runs(run_id), ordinal INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(run_id,ordinal));
          CREATE TABLE IF NOT EXISTS agent_results (
            run_id TEXT PRIMARY KEY REFERENCES agent_runs(run_id), payload TEXT NOT NULL);
        ''')
        self.connection.commit()

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _open(self, run_id):
        if not self.connection.execute("SELECT 1 FROM agent_runs WHERE run_id=?", (run_id,)).fetchone():
            raise KeyError("run_not_found")
        if self.connection.execute("SELECT 1 FROM agent_results WHERE run_id=?", (run_id,)).fetchone():
            raise ValueError("run_sealed")

    def create_run(self, run_id, context, parent_run_id=None):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be nonempty")
        payload = _encode(context)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connection:
            if parent_run_id is not None and not self.connection.execute("SELECT 1 FROM agent_runs WHERE run_id=?", (parent_run_id,)).fetchone():
                raise KeyError("parent_run_not_found")
            self.connection.execute("INSERT INTO agent_runs VALUES (?,?,?,?,?)", (run_id,parent_run_id,payload,digest,datetime.now(timezone.utc).isoformat()))
        return run_id

    def save_artifact(self, run_id, kind, payload):
        if not isinstance(kind, str) or not kind:
            raise ValueError("artifact kind required")
        encoded = _encode(payload)
        artifact_id = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self._open(run_id)
            self.connection.execute("INSERT OR IGNORE INTO agent_artifacts VALUES (?,?)", (artifact_id,encoded))
            self.connection.execute("INSERT INTO agent_run_artifacts SELECT ?,COALESCE(MAX(ordinal),0)+1,?,? FROM agent_run_artifacts WHERE run_id=?", (run_id,kind,artifact_id,run_id))
        return artifact_id

    def record_step(self, run_id, step):
        encoded = _encode(step)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self._open(run_id)
            self.connection.execute("INSERT INTO agent_steps SELECT ?,COALESCE(MAX(ordinal),0)+1,? FROM agent_steps WHERE run_id=?", (run_id,encoded,run_id))

    def finish_run(self, run_id, result):
        encoded = _encode(result)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self._open(run_id)
            self.connection.execute("INSERT INTO agent_results VALUES (?,?)", (run_id,encoded))

    def has_prior_run(self, *, company_id, event_id, role):
        # Only completed observations count; an in-progress current run does not.
        rows = self.connection.execute('SELECT r.context FROM agent_runs r JOIN agent_results f ON r.run_id=f.run_id')
        for row in rows:
            context = _decode(row[0])
            if (context.get('profile',{}).get('company_id') == company_id and
                context.get('snapshot',{}).get('event_id') == event_id and
                context.get('role') == role):
                return True
        return False

    def get_artifact(self, artifact_id):
        row = self.connection.execute("SELECT payload FROM agent_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError("artifact_not_found")
        if hashlib.sha256(row[0].encode()).hexdigest() != artifact_id:
            raise ValueError("artifact_hash_mismatch")
        return _decode(row[0])

    def get_run(self, run_id):
        row = self.connection.execute("SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("run_not_found")
        if hashlib.sha256(row['context'].encode()).hexdigest() != row['context_sha256']:
            raise ValueError("context_hash_mismatch")
        artifacts = [{"kind": a['kind'], "artifact_id": a['artifact_id'], "payload": self.get_artifact(a['artifact_id'])} for a in self.connection.execute("SELECT * FROM agent_run_artifacts WHERE run_id=? ORDER BY ordinal", (run_id,))]
        result = self.connection.execute("SELECT payload FROM agent_results WHERE run_id=?", (run_id,)).fetchone()
        return {"run_id":run_id,"parent_run_id":row['parent_run_id'],"created_at":row['created_at'],"context":_decode(row['context']),"context_sha256":row['context_sha256'],"artifacts":artifacts,"steps":[_decode(s[0]) for s in self.connection.execute("SELECT payload FROM agent_steps WHERE run_id=? ORDER BY ordinal",(run_id,))],"result":_decode(result[0]) if result else None}
