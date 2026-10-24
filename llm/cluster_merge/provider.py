"""Explicit one-shot transport and shared request-quota adapter."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path


class ProviderError(Exception):
    pass


class SQLiteQuota:
    """Use the SAME file and limit in every process sharing the experiment quota."""

    def __init__(self, path, limit):
        if limit <= 0:
            raise ValueError("quota must be positive")
        self.path, self.limit = str(path), limit
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), quota INTEGER NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (limit,))
            db.commit()
            if db.execute("SELECT quota FROM settings").fetchone()[0] != limit:
                raise ValueError("shared_quota_limit_mismatch")

    def reserve(self, key):
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=5)) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM reservations WHERE id=?", (key,)).fetchone():
                raise ProviderError("reservation_already_consumed")
            if (
                db.execute("SELECT count(*) FROM reservations").fetchone()[0]
                >= self.limit
            ):
                raise ProviderError("quota_exhausted")
            db.execute("INSERT INTO reservations(id) VALUES (?)", (key,))
            db.commit()


class OpenAIProvider:
    def complete(self, request, profile):
        import httpx
        from openai import OpenAI

        key = os.environ.get("LLM_API_KEY")
        if not key:
            raise ProviderError("api_key_missing")
        with OpenAI(
            api_key=key,
            base_url=profile.endpoint,
            max_retries=0,
            timeout=httpx.Timeout(180, connect=5),
            http_client=httpx.Client(follow_redirects=False, trust_env=False),
        ) as client:
            try:
                response = client.chat.completions.create(
                    model=profile.model,
                    messages=request,
                    max_tokens=profile.max_output_tokens,
                    extra_body={"reasoning": {"enabled": profile.reasoning}},
                )
                import json

                return json.loads(
                    json.dumps(response.model_dump(mode="json")).replace(
                        key, "[REDACTED]"
                    )
                )
            except Exception as exc:  # noqa: BLE001 -- redact provider exception details
                # Never persist SDK exception bodies, headers or credentials.
                raise ProviderError(type(exc).__name__) from None
