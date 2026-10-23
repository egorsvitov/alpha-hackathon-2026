"""Persistent, leased single-executor operations. No model retries."""

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .core import (
    canonical,
    configuration,
    digest,
    event_from,
    fits,
    messages,
    parse_answer,
    plan_merge,
)
from .models import Disposition, Event, MergeRequest, MergeResult


class MergeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class MergeService:
    def __init__(
        self,
        provider,
        limiter,
        path="data/processed/cluster_merge/state.sqlite3",
        *,
        clock=time.time,
    ):
        self.provider, self.limiter, self.path, self.clock = (
            provider,
            limiter,
            str(path),
            clock,
        )
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, request TEXT NOT NULL,
                owner TEXT, expires REAL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, operation TEXT NOT NULL,
                request TEXT NOT NULL, response TEXT, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS results(id TEXT PRIMARY KEY, operation TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    def plan_merge(self, request):
        return plan_merge(request)

    def get_merge_result(self, result_id):
        with self.db() as db:
            row = db.execute(
                "SELECT payload FROM results WHERE id=?", (result_id,)
            ).fetchone()
        if not row:
            raise MergeError("result_not_found")
        return MergeResult.model_validate_json(row[0])

    def resume_merge(self, operation_id, *, max_calls):
        with self.db() as db:
            row = db.execute(
                "SELECT request FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
        if not row:
            raise MergeError("operation_not_found")
        return self.merge_cluster(
            MergeRequest.model_validate_json(row[0]), max_calls=max_calls
        )

    def _save(self, op, owner, state):
        with self.db() as db:
            n = db.execute(
                "UPDATE operations SET state=?, expires=? WHERE id=? AND owner=? AND expires>?",
                (canonical(state), self.clock() + 900, op, owner, self.clock()),
            ).rowcount
        if not n:
            raise MergeError("lease_lost")

    def merge_cluster(self, request, *, max_calls):
        if (
            isinstance(max_calls, bool)
            or not isinstance(max_calls, int)
            or max_calls < 0
        ):
            raise ValueError("max_calls must be a nonnegative integer")
        plan = plan_merge(request)
        op, owner = plan.operation_id, str(uuid.uuid4())
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO operations VALUES (?,?,NULL,0,?)",
                (
                    op,
                    request.model_dump_json(),
                    canonical(
                        {
                            "batch": 0,
                            "events": [],
                            "coverage": {},
                            "pairs": {},
                            "blocked": plan.blocked,
                        }
                    ),
                ),
            )
            row = db.execute(
                "SELECT owner,expires,state FROM operations WHERE id=?", (op,)
            ).fetchone()
            state = json.loads(row[2])
            if row[0] and row[1] > self.clock():
                db.rollback()
                raise MergeError("in_progress")
            db.execute(
                "UPDATE operations SET owner=?,expires=? WHERE id=?",
                (owner, self.clock() + 900, op),
            )
            db.commit()
        stop = threading.Event()
        lost = threading.Event()

        def heartbeat():
            while not stop.wait(30):
                try:
                    with self.db() as db:
                        n = db.execute(
                            "UPDATE operations SET expires=? WHERE id=? AND owner=? AND expires>?",
                            (self.clock() + 900, op, owner, self.clock()),
                        ).rowcount
                    if not n:
                        lost.set()
                        return
                except sqlite3.Error:
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        remaining = max_calls
        cache_hit = True
        allfs = {f.id: f for f in plan.fragments}
        failure = None

        def call(fs):
            nonlocal remaining, cache_hit
            req = messages(request, fs)
            cid = digest([op, req])
            with self.db() as db:
                row = db.execute(
                    "SELECT response,status FROM calls WHERE id=?", (cid,)
                ).fetchone()
            if row:
                if row[1] != "response":
                    raise MergeError("unknown_or_failed_call")
                response = json.loads(row[0])
            else:
                if remaining <= 0:
                    raise MergeError("call_limit")
                if lost.is_set():
                    raise MergeError("lease_lost")
                self._save(op, owner, state)
                # Persist intent BEFORE quota reservation/network; uncertain crash consumes this attempt.
                with self.db() as db:
                    db.execute(
                        "INSERT INTO calls VALUES (?,?,?,NULL,?)",
                        (cid, op, canonical(req), "reserved"),
                    )
                try:
                    self.limiter.reserve(cid)
                except Exception:  # noqa: BLE001 -- preserve uncertain expense without retry
                    with self.db() as db:
                        db.execute(
                            "DELETE FROM calls WHERE id=? AND status=?",
                            (cid, "reserved"),
                        )
                    raise MergeError("quota_reservation_failed") from None
                remaining -= 1
                cache_hit = False
                try:
                    response = self.provider.complete(req, request.profile)
                except Exception:  # noqa: BLE001 -- preserve uncertain expense without retry
                    with self.db() as db:
                        db.execute(
                            "UPDATE calls SET status=? WHERE id=?", ("unknown", cid)
                        )
                    raise MergeError("provider_failure_expense_unknown") from None
                # Late response is audit-only until the live owner checks its lease.
                with self.db() as db:
                    db.execute(
                        "UPDATE calls SET response=?,status=? WHERE id=?",
                        (canonical(response), "response", cid),
                    )
            self._save(op, owner, state)
            try:
                choice = response["choices"][0]
                message = choice["message"]
                if choice["finish_reason"] != "stop":
                    raise ValueError("finish_reason_" + str(choice["finish_reason"]))
                if (
                    message.get("refusal")
                    or message.get("tool_calls")
                    or not message.get("content")
                ):
                    raise ValueError("invalid_message")
                answer = parse_answer(message["content"], fs)
                if any(c.kind == "unresolved" for c in answer.coverage):
                    raise ValueError("unresolved_content")
                return answer
            except (ValueError, KeyError, IndexError, TypeError):
                raise MergeError("invalid_or_incomplete_answer") from None

        try:
            while state["batch"] < len(plan.batches):
                fs = [allfs[x] for x in plan.batches[state["batch"]]]
                answer = call(fs)
                state["events"].extend(
                    event_from(d, answer, fs, request, op).model_dump()
                    for d in answer.events
                )
                state["coverage"].update(
                    {c.fragment_id: c.model_dump() for c in answer.coverage}
                )
                state["batch"] += 1
                self._save(op, owner, state)
            # Complete-link evidence is always originals. Restart pair scan after any composition change.
            while True:
                events = sorted(
                    [Event.model_validate(e) for e in state["events"]],
                    key=lambda e: e.id,
                )
                pending = [
                    (a, b)
                    for i, a in enumerate(events)
                    for b in events[i + 1 :]
                    if digest(sorted([a.id, b.id])) not in state["pairs"]
                ]
                if not pending:
                    break
                a, b = pending[0]
                pair = digest(sorted([a.id, b.id]))
                fs = sorted(
                    {f.id: f for f in a.fragments + b.fragments}.values(),
                    key=lambda f: f.id,
                )
                if not fits(request, fs):
                    state["pairs"][pair] = "context_too_large"
                    self._save(op, owner, state)
                    continue
                answer = call(fs)
                if len(answer.events) == 1:
                    merged = event_from(answer.events[0], answer, fs, request, op)
                    expected = {f.id for f in a.fragments + b.fragments}
                    if {f.id for f in merged.fragments} != expected:
                        raise MergeError("pair_merge_dropped_material")
                    state["events"] = [
                        e.model_dump() for e in events if e.id not in (a.id, b.id)
                    ] + [merged.model_dump()]
                    state["coverage"].update(
                        {c.fragment_id: c.model_dump() for c in answer.coverage}
                    )
                else:
                    # A comparison may confirm two intact groups; repartition is not silently ignored.
                    old = {frozenset(f.id for f in e.fragments) for e in (a, b)}
                    new = {
                        frozenset(
                            f.id
                            for f in event_from(d, answer, fs, request, op).fragments
                        )
                        for d in answer.events
                    }
                    state["pairs"][pair] = (
                        "different" if old == new else "unresolved_repartition"
                    )
                self._save(op, owner, state)
        except MergeError as exc:
            failure = exc.code
        finally:
            stop.set()
            thread.join()
        unresolved = list(state["blocked"])
        active_ids = {e["id"] for e in state["events"]}
        for pair, outcome in state["pairs"].items():
            if outcome != "different" and any(
                digest(sorted([a, b])) == pair
                for a in active_ids
                for b in active_ids
                if a < b
            ):
                unresolved.append(outcome)
        if failure:
            unresolved.append(failure)
        done = state["batch"] == len(plan.batches) and not unresolved
        status = (
            "completed"
            if done
            else ("deferred" if state["blocked"] and not state["events"] else "partial")
        )
        if failure and failure not in ("call_limit", "quota_reservation_failed"):
            status = "failed"
        with self.db() as db:
            rows = db.execute(
                "SELECT response FROM calls WHERE operation=?", (op,)
            ).fetchall()
        usage = [
            json.loads(r[0]).get("usage", {}) if r[0] else {"expense": "unknown"}
            for r in rows
        ]
        result = MergeResult(
            id="",
            operation_id=op,
            input_hash=plan.input_hash,
            configuration=configuration(request),
            status=status,
            mode=(
                "merged"
                if len(state["events"]) == 1 and len(request.articles) > 1
                else "unchanged"
                if len(state["events"]) <= 1
                else "split"
            )
            if done
            else None,
            events=state["events"],
            coverage=[
                Disposition.model_validate(v) for v in state["coverage"].values()
            ],
            unresolved=unresolved,
            calls=len(rows),
            usage=usage,
            cache_hit=cache_hit,
            expected_cluster_id=request.cluster_id,
            expected_revision=request.revision,
        )
        result.id = digest(result.model_dump(exclude={"id", "cache_hit"}))
        try:
            self._save(op, owner, state)
            with self.db() as db:
                db.execute(
                    "INSERT OR IGNORE INTO results VALUES (?,?,?)",
                    (result.id, op, result.model_dump_json()),
                )
        finally:
            with self.db() as db:
                db.execute(
                    "UPDATE operations SET owner=NULL,expires=0 WHERE id=? AND owner=?",
                    (op, owner),
                )
        return result
