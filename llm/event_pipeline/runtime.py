"""Small boundary to the existing archive, one-shot execution and accounting."""

from pathlib import Path
from llm.events.storage.store import EventStore, canonical_json, StorageError
from llm.events.storage.models import ProcessingSpec
from llm.events.extraction.models import ProviderProfile, ExecutionLimits
from llm.events.extraction.runner import OneShotRunner
from llm.events.extraction.provider import (
    OfflineProvider,
    ProviderResponse,
    OpenAIProvider,
)
from .extraction import decode
import sqlite3
import time
from contextlib import closing
from llm.events.extraction.credits import RequestCounter


class CreditLedger:
    """Reuse an existing counter, never create/reset its settings."""

    def __init__(self, store, path):
        self.store = store
        self.counter = object.__new__(RequestCounter)
        self.counter.path = Path(path).resolve()
        with closing(
            sqlite3.connect(self.counter.path.as_uri() + "?mode=ro", uri=True)
        ) as db:
            row = db.execute("SELECT max_requests FROM settings WHERE id=1").fetchone()
            if row is None or row[0] < 1:
                raise StorageError("invalid_quota_journal", "Existing quota required")
            db.execute("SELECT call_id FROM calls LIMIT 1")
        self.started = {}

    def reserve(self, token, request, profile, limits, guard=None):
        self.store.renew_lease(token)
        try:
            cid = self.counter.reserve(request)
        except StorageError as exc:
            raise StorageError("budget_quota_exhausted", str(exc)) from None
        self.started[cid] = time.monotonic()
        self.store.save_artifact(
            token,
            "shared_quota_reservation",
            {"call_id": cid, "journal": str(self.counter.path)},
        )
        return cid

    def finish(self, cid, response=None, error=None):
        self.counter.finish(cid, response, error, time.monotonic() - self.started[cid])
        return {
            "state": "settled"
            if response and response.get("body", {}).get("usage")
            else "unknown"
        }

    def budget(self):
        return {"blocked": False}


class Runner(OneShotRunner):
    def __init__(self, *args, journal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.credit_mode = journal is not None
        if journal is not None:
            if not self.provider.is_live:
                raise StorageError(
                    "invalid_quota_mode", "Offline tests must not consume live quota"
                )
            self.ledger = CreditLedger(self.store, journal)

    def _preflight(self, spec, limits, **kwargs):
        if not self.credit_mode:
            return super()._preflight(spec, limits, **kwargs)
        profile = self.profile_for_spec(spec)
        if not limits.live_enabled or profile.execution_mode != "live":
            raise StorageError("live_calls_disabled", "Explicit live mode required")
        if profile.model != "deepseek/deepseek-v4.1-flash" or profile.max_tokens > 4096:
            raise StorageError("invalid_profile", "Use approved model and output cap")
        if self.provider.profile != profile:
            raise StorageError("provider_profile_mismatch", "Profile differs")
        if profile.response_format != "text" and not profile.format_verified:
            raise StorageError(
                "unverified_response_format", "Explicit verification required"
            )
        if (
            profile.thinking is not None or profile.reasoning_effort is not None
        ) and not profile.thinking_verified:
            raise StorageError("unverified_reasoning", "Explicit verification required")
        self.provider.check_credentials()
        return profile

    def profile_for_spec(self, spec):
        self.spec = spec
        return ProviderProfile.model_validate(spec.configuration["profile"])

    def request_for(self, record, profile):
        return self.spec.configuration["request"]

    def output_for(self, content, record, spec):
        # Save response before semantic parsing. Bad records remain auditable.
        try:
            decode(content)
        except (ValueError, TypeError):
            raise StorageError("invalid_json", "Response is not valid JSON") from None
        return (
            {"content": content, "source_id": record.source_id},
            "pipeline_response",
            "event-pipeline.v1",
        )


def request(profile, prompt, data):
    result = {
        "model": profile.model,
        "max_tokens": profile.max_tokens,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": canonical_json(data)},
        ],
    }
    if profile.response_format != "text":
        result["response_format"] = {"type": "json_object"}
        if profile.response_format == "json_schema":
            field = "mentions" if "primary" in data else "decisions"
            result["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "pipeline_" + field,
                    "strict": False,
                    "schema": {
                        "type": "object",
                        "properties": {
                            field: {"type": "array", "items": {"type": "object"}}
                        },
                        "required": [field],
                    },
                },
            }
    if profile.thinking is not None:
        result["thinking"] = {"type": profile.thinking}
    if profile.reasoning_effort is not None:
        result["reasoning_effort"] = profile.reasoning_effort
    return result


def execute(
    store, source_id, stage, request_data, profile, provider, limits=None, journal=None
):
    spec = ProcessingSpec(
        processor_name="event_pipeline." + stage,
        processor_version="1",
        configuration={
            "profile": profile.model_dump(mode="json"),
            "request": request_data,
            "implementation": {
                p.name: __import__("hashlib").sha256(p.read_bytes()).hexdigest()
                for p in Path(__file__).parent.glob("*.py")
            },
        },
    )
    job = store.enqueue(source_id, spec)
    if job.state == "succeeded":
        return {"status": "cached", "artifact_id": job.result_artifact_id, "calls": 0}
    if job.state != "pending":
        return {"status": job.state, "reason": "explicit_retry_required", "calls": 0}
    if limits is None and not provider.is_live:
        limits = ExecutionLimits(
            tariffs_confirmed=True,
            input_rub_per_million="0",
            output_rub_per_million="0",
            daily_quota_rub="1",
        )
    result = Runner(store, provider, journal=journal).run_next(
        spec, limits or ExecutionLimits(), source_id=source_id
    )
    return {
        **result.model_dump(),
        "artifact_id": store.get_job(job.job_id).result_artifact_id,
        "calls": int(result.call_id is not None),
    }


__all__ = [
    "EventStore",
    "ProviderProfile",
    "ExecutionLimits",
    "OfflineProvider",
    "ProviderResponse",
    "OpenAIProvider",
    "canonical_json",
    "execute",
    "request",
    "Runner",
    "CreditLedger",
]
