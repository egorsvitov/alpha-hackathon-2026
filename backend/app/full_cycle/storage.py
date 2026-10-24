"""Private durable journal. Readers see complete jobs and committed index pointers."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from retriever.models import canonical


def now():
    return datetime.now(timezone.utc).isoformat()


class PipelineError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Workspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / 'pipeline.sqlite3'
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS company_versions(company_id TEXT, version TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(company_id,version));
            CREATE TABLE IF NOT EXISTS interest_versions(company_id TEXT, version TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(company_id,version));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, company_id TEXT NOT NULL, role TEXT NOT NULL,
                created TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cards(id TEXT PRIMARY KEY, company_id TEXT NOT NULL, role TEXT NOT NULL,
                canonical_event_id TEXT NOT NULL, context_hash TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(company_id,role,canonical_event_id,context_hash));
            CREATE TABLE IF NOT EXISTS job_cards(job_id TEXT, card_id TEXT, PRIMARY KEY(job_id,card_id));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def writer(self):
        # Shared across CLI processes; a killed worker releases the lock automatically.
        with (self.root / 'worker.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def state(self, key):
        with self.db() as db:
            row = db.execute('SELECT payload FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_state(self, key, payload):
        with self.db() as db:
            self._state(db, key, payload)

    @staticmethod
    def _state(db, key, payload):
        db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, canonical(payload)))

    @staticmethod
    def immutable(db, table, keys, payload):
        cols = {'revisions': ('id',), 'company_versions': ('company_id','version'),
                'interest_versions': ('company_id','version')}[table]
        where = ' AND '.join(c+'=?' for c in cols)
        row = db.execute(f'SELECT payload FROM {table} WHERE {where}', keys).fetchone()
        value = canonical(payload)
        if row and row[0] != value:
            raise PipelineError('immutable_' + table + '_conflict')
        if not row:
            db.execute(f'INSERT INTO {table} VALUES ({",".join("?" for _ in (*keys,value))})', (*keys,value))
        return not bool(row)

    def job(self, job_id):
        with self.db() as db:
            row = db.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def publish_job(self, job, cards=()):
        with self.db() as db:
            for card in cards:
                db.execute('INSERT OR IGNORE INTO cards VALUES (?,?,?,?,?,?)',
                           (card['id'], job['company_id'], job['role'], card['canonical_event_id'],
                            card['context_hash'], canonical(card)))
                db.execute('INSERT OR IGNORE INTO job_cards VALUES (?,?)', (job['id'], card['id']))
            db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?)',
                       (job['id'],job['company_id'],job['role'],job['created_at'],canonical(job)))
            self._state(db, 'latest:' + job['company_id'] + ':' + job['role'], {'id': job['id']})

    def latest(self, company_id, role):
        ref = self.state('latest:' + company_id + ':' + role)
        return self.job(ref['id']) if ref else None

    def cards_for(self, job_id):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute(
                'SELECT c.payload FROM cards c JOIN job_cards j ON c.id=j.card_id WHERE j.job_id=? ORDER BY c.id', (job_id,))]
