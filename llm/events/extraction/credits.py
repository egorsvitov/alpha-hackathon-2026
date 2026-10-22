"""Explicit request-count pilot for prepaid/credit access with unknown RUB tariffs.

This entry point does not relax the production RUB budget gates. It uses a
separate persistent request counter; a reservation consumes a slot even on crash.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from llm.events.extraction.protocol import build_processing_spec, build_request
from llm.events.contracts.models import Issue
from llm.events.extraction.provider import ProviderFailure
from llm.events.extraction.runner import ExtractionRunner, LeaseHeartbeat
from llm.events.storage.store import StorageError, canonical_json
from llm.events.contracts.validation import ContractValidationError


class RequestCounter:
    """One immutable total request cap per persistent pilot journal, not per run."""
    MAX_REQUESTS = 3

    def __init__(self, path, limit):
        if type(limit) is not int or not 1 <= limit <= self.MAX_REQUESTS:
            raise StorageError('invalid_pilot_limit', f'Pilot allows 1–{self.MAX_REQUESTS} total requests')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY, max_requests INTEGER NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS calls (call_id TEXT PRIMARY KEY, created_at REAL NOT NULL, '
                'request_json TEXT NOT NULL, response_json TEXT, error_json TEXT, elapsed_seconds REAL)')
            conn.execute('INSERT OR IGNORE INTO settings VALUES (1,?)', (limit,))
            if conn.execute('SELECT max_requests FROM settings WHERE id=1').fetchone()[0] != limit:
                raise StorageError('pilot_limit_mismatch', 'Do not change the existing pilot request cap')

    def reserve(self, request):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            limit = conn.execute('SELECT max_requests FROM settings WHERE id=1').fetchone()[0]
            if conn.execute('SELECT COUNT(*) FROM calls').fetchone()[0] >= limit:
                conn.rollback()
                raise StorageError('pilot_limit_exhausted', 'All pilot request slots are consumed')
            call_id = str(uuid4())
            conn.execute('INSERT INTO calls(call_id,created_at,request_json) VALUES (?,?,?)',
                (call_id, time.time(), canonical_json(request)))
            conn.commit()
            return call_id

    def finish(self, call_id, response, error, elapsed):
        if (response is None) == (error is None):
            raise StorageError('invalid_pilot_outcome', 'Supply exactly one response or error')
        with closing(sqlite3.connect(self.path)) as conn, conn:
            changed = conn.execute('UPDATE calls SET response_json=?,error_json=?,elapsed_seconds=? '
                'WHERE call_id=? AND elapsed_seconds IS NULL',
                (canonical_json(response) if response is not None else None,
                 canonical_json(error) if error is not None else None, elapsed, call_id)).rowcount
            if changed != 1:
                raise StorageError('pilot_call_already_finished', call_id)

    def summary(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute('SELECT * FROM calls ORDER BY created_at')]
        for row in rows:
            row['request'] = json.loads(row.pop('request_json'))
            response = row.pop('response_json')
            error = row.pop('error_json')
            row['response'] = json.loads(response) if response else None
            row['error'] = json.loads(error) if error else None
            row['cost_rub'] = None
        return rows


def extract_one(store, record, profile, counter, provider, *, processor=None):
    """Real B3 request, parser and artifacts; experimental quota replaces RUB accounting."""
    if profile.execution_mode != 'live' or profile.model != 'deepseek/deepseek-v4.1-flash':
        raise StorageError('invalid_pilot_profile', 'Use the supplied live model')
    if not 1 <= profile.max_tokens <= 4096:
        raise StorageError('invalid_pilot_profile', 'Pilot response cap is 4096 tokens')
    if provider.profile != profile:
        raise StorageError('provider_profile_mismatch', 'Provider differs from the recorded profile')
    provider.check_credentials()
    spec = processor.build_spec(profile) if processor is not None else build_processing_spec(profile)
    job = store.enqueue(record.source_id, spec)
    if job.state == 'succeeded':
        return {'status': 'cached', 'job_id': job.job_id, 'artifact_id': job.result_artifact_id}
    if job.state != 'pending':
        return {'status': job.state, 'job_id': job.job_id, 'reason': 'explicit_retry_required'}
    request = processor.request_for(record, profile) if processor is not None else build_request(record, profile)
    if len(canonical_json(request).encode()) > profile.max_request_bytes:
        store.defer_job(job.job_id, 'input_too_large')
        return {'status': 'deferred', 'reason': 'input_too_large'}
    running = store.claim_next(spec, 'explicit-credit-pilot', source_id=record.source_id)
    if running is None:
        return {'status': 'running', 'job_id': job.job_id}
    token = running.active_attempt.token
    try:
        call_id = counter.reserve(request)
    except StorageError:
        store.defer_job(job.job_id, 'pilot_limit_exhausted', token=token)
        raise
    store.save_artifact(token, 'pilot_quota_reservation',
        {'call_id': call_id, 'quota_kind': 'request_count', 'cost_rub': None,
         'tariffs_confirmed': False, 'journal': str(counter.path)})
    store.save_artifact(token, 'llm_request', request, schema_version='b3.1')
    started = time.monotonic()
    with LeaseHeartbeat(store, token) as heartbeat:
        store.renew_lease(token)
        try:
            response = provider.complete(request)
        except ProviderFailure as exc:
            counter.finish(call_id, None, {'code': exc.code}, time.monotonic() - started)
            try:
                store.fail_attempt(token, [Issue(code=exc.code, path='/', message=str(exc))])
            except StorageError:
                pass  # The quota journal remains valid even after losing the attempt.
            return {'status': 'failed', 'call_id': call_id, 'reason': exc.code, 'stop': True}
        counter.finish(call_id, response.payload(), None, time.monotonic() - started)
    if heartbeat.lost:
        return {'status': 'superseded', 'call_id': call_id, 'stop': True}
    try:
        store.save_artifact(token, 'llm_response', response.payload(), schema_version='b3.1')
        if response.status_code != 200:
            code = 'provider_rate_limited' if response.status_code == 429 else 'provider_http_error'
            store.fail_attempt(token, [Issue(code=code, path='/', message=str(response.status_code))])
            return {'status': 'failed', 'call_id': call_id, 'reason': code, 'stop': True}
        choices = response.body.get('choices', [])
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise StorageError('invalid_provider_response', 'Expected one choice')
        choice = choices[0]
        message = choice.get('message') or {}
        if choice.get('finish_reason') != 'stop':
            raise StorageError('output_limit_exceeded' if choice.get('finish_reason') == 'length'
                               else 'unexpected_finish_reason', 'Response did not finish normally')
        if message.get('refusal') or message.get('tool_calls') or message.get('function_call'):
            raise StorageError('provider_refusal_or_tools', 'No extraction result')
        worker = processor if processor is not None else ExtractionRunner(store, provider)
        payload, kind, version = worker.output_for(message.get('content'), record, spec)
        worker.save_output_diagnostics(message.get('content'), record, spec, token)
        completed = store.complete_attempt(token, payload, kind=kind, schema_version=version)
        return {'status': 'succeeded', 'job_id': job.job_id, 'call_id': call_id,
                'artifact_id': completed.result_artifact_id}
    except ContractValidationError as exc:
        if getattr(exc, 'code', None) == 'stale_attempt':
            return {'status': 'superseded', 'call_id': call_id, 'stop': True}
        store.save_artifact(token, 'extraction_errors', [i.model_dump() for i in exc.issues])
        store.fail_attempt(token, exc.issues)
        return {'status': 'failed', 'call_id': call_id, 'issues': [i.model_dump() for i in exc.issues]}
