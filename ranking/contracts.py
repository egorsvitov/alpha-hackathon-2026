"""Shared strict JSON contracts, safe errors, timestamps and snapshot types."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol
from functools import lru_cache

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = PROJECT_ROOT / "docs" / "contracts"

@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str
    severity: str = "error"
    def to_dict(self):
        return asdict(self)

class ContractError(ValueError):
    code = "contract_error"
    def __init__(self, message=None, *, issues=()):
        self.issues = tuple(issues)
        super().__init__(message or self.code)

class ContractValidationError(ContractError):
    code = "contract_validation_error"
    def __init__(self, issues):
        super().__init__(issues=issues)

class BundleValidationError(ContractValidationError):
    code = "bundle_validation_error"

class InputError(ContractError):
    code = "input_error"

class EventVersionCollision(ContractError):
    code = "event_version_collision"

class EventNotFound(ContractError):
    code = "event_not_found"


def parse_timestamp(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return result.astimezone(timezone.utc)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

canonical_hash = payload_hash


def pointer_path(parts) -> str:
    return "".join("/" + str(x).replace("~", "~0").replace("/", "~1") for x in parts)


def resolve_pointer(value: Any, path: str) -> Any:
    if path == "":
        return value
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("invalid JSON Pointer")
    import re
    for token in path[1:].split("/"):
        if re.search(r"~(?![01])", token):
            raise ValueError("invalid JSON Pointer escape")
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", token):
                raise ValueError("invalid array index")
            value = value[int(token)]
        elif isinstance(value, dict):
            value = value[token]
        else:
            raise ValueError("pointer traverses scalar")
    return value


@lru_cache(maxsize=None)
def _validator(name):
    schemas = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in SCHEMA_DIR.glob("*.schema.json")}
    registry = Registry()
    for filename, schema in schemas.items():
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(filename, resource).with_resource((SCHEMA_DIR / filename).as_uri(), resource)
        if "$id" in schema:
            registry = registry.with_resource(schema["$id"], resource)
    schema = schemas[name]
    return Draft202012Validator(schema, format_checker=FormatChecker(), registry=registry)


def validate_contract(value, schema_name: str) -> list[ValidationIssue]:
    name = schema_name if schema_name.endswith(".schema.json") else schema_name + ".schema.json"
    if name == "event-bundle.schema.json" and isinstance(value, dict):
        version = value.get("schema_version")
        if version == "0.2":
            name = "event-bundle.v0.2.schema.json"
        elif version not in (None, "0.1"):
            return [ValidationIssue("unsupported_contract_version", "/schema_version", "Supported EventBundle versions: 0.1, 0.2")]
    validator = _validator(name)
    issues = [ValidationIssue("schema_error", pointer_path(e.absolute_path), f"Schema constraint failed: {e.validator}") for e in validator.iter_errors(value)]
    try:
        canonical_json(value)
    except (ValueError, TypeError):
        issues.append(ValidationIssue("non_finite_json", "", "JSON contains unsupported/non-finite values"))
    if issues:
        return issues
    if name == "relevance.schema.json":
        issues.extend(validate_relevance(value))
    if name == "company-profile.schema.json":
        issues.extend(validate_profile(value))
    if name == "ranking-result.schema.json":
        for i, item in enumerate(value["ranked"]):
            issues.extend(ValidationIssue(x.code, f"/ranked/{i}/relevance" + x.path, x.message, x.severity) for x in validate_relevance(item["relevance"]))
        ranked = {canonical_json(x) for x in value["ranked"]}
        if any(canonical_json(x) not in ranked for x in value["feed"]):
            issues.append(ValidationIssue("feed_not_subset", "/feed", "Feed must be a subset of ranked"))
    if name == "company-profiles.schema.json":
        for i, profile in enumerate(value["profiles"]):
            issues.extend(ValidationIssue(x.code, f"/profiles/{i}" + x.path, x.message, x.severity) for x in validate_profile(profile))
        ids = [p["company_id"] for p in value["profiles"]]
        if len(set(ids)) != len(ids):
            issues.append(ValidationIssue("duplicate_company_id", "/profiles", "company_id must be unique"))
    return issues


def validate_profile(value):
    issues = []
    def bad(code, path, message):
        issues.append(ValidationIssue(code, path, message))
    features = value["features"]
    for key, feature in features.items():
        path = "/features" + pointer_path([key])
        if feature["feature_id"] != key:
            bad("feature_id_mismatch", path, "Feature dictionary key must equal feature_id")
        try:
            target = resolve_pointer(value, feature["path"])
            if target != feature["value"]:
                bad("feature_value_mismatch", path, "Feature value differs from its profile path")
        except (ValueError, IndexError, KeyError, TypeError):
            bad("feature_pointer_invalid", path, "Feature path does not resolve")
        origin = feature["origin"]
        if origin["kind"] == "derived":
            if key in origin["input_feature_ids"] or any(ref not in features for ref in origin["input_feature_ids"]):
                bad("feature_reference_invalid", path, "Derived feature references must resolve and cannot refer to self")
            if feature["path"].startswith("/graph/") and (origin["artifact_id"] is None or origin["artifact_sha256"] is None):
                bad("graph_artifact_missing", path, "Derived graph feature requires artifact provenance")
    graph = value["graph"]
    if graph["graph_coverage"] is not None:
        from decimal import Decimal
        import math
        for direction, coverage in graph["graph_coverage"].items():
            total = coverage["input_records"]
            retained = coverage["retained_records"]
            ambiguous = coverage["excluded_ambiguous_records"]
            invalid = coverage["excluded_invalid_records"]
            fraction = coverage["retained_record_fraction"]
            expected_quality = "unavailable" if retained == 0 else ("limited" if ambiguous or invalid else "clean_observed")
            if total != retained + ambiguous + invalid or ((fraction is None) != (total == 0)) or (total and fraction is not None and not math.isclose(fraction, retained / total, abs_tol=1e-6)):
                bad("graph_coverage_arithmetic", "/graph/graph_coverage/" + direction, "Graph record counters and fraction disagree")
            # Global unassignable records may conservatively downgrade clean to limited.
            if coverage["share_quality"] != expected_quality and not (expected_quality == "clean_observed" and coverage["share_quality"] == "limited"):
                bad("graph_share_quality", "/graph/graph_coverage/" + direction, "Graph share quality disagrees with retained records/losses")
            amount_fraction = coverage["retained_positive_amount_fraction"]
            denominator = Decimal(coverage["valid_positive_input_amount_raw"])
            amount = Decimal(coverage["retained_amount_raw"])
            if amount < 0 or denominator < 0 or (amount_fraction is not None and (denominator <= 0 or not math.isclose(amount_fraction, float(amount / denominator), abs_tol=1e-6))):
                bad("graph_coverage_arithmetic", "/graph/graph_coverage/" + direction, "Graph amount fraction and denominator disagree")
    if (graph["status"] == "loaded") != (graph["graph_coverage"] is not None):
        bad("graph_coverage_status", "/graph/graph_coverage", "Graph coverage must be present exactly for loaded graph")
    snapshot = value["source_snapshot"]
    if snapshot["historical_applicability"] == "dated_snapshot" and snapshot["data_as_of"] is None:
        bad("snapshot_date_missing", "/source_snapshot/data_as_of", "Dated snapshot requires a confirmed date")
    if snapshot["data_as_of"] is not None:
        origins = [f["origin"] for f in features.values() if f["path"] == "/source_snapshot/data_as_of"]
        if not origins or any(o["kind"] != "manifest" for o in origins):
            bad("snapshot_date_provenance", "/source_snapshot/data_as_of", "Confirmed snapshot date requires manifest provenance")
    return issues


def require_valid_contract(value, schema_name):
    issues = validate_contract(value, schema_name)
    if any(i.severity == "error" for i in issues):
        raise ContractValidationError(issues)
    return issues


def validate_relevance(value):
    import math
    issues = []
    def bad(code, path, message):
        issues.append(ValidationIssue(code, path, message))
    score = value["score"]
    if score is None:
        if value["level"] != "unknown" or value["raw_total"] is not None or value["scoring_diagnostics"] is not None:
            bad("unscored_consistency", "/score", "Unscored relevance requires unknown level and null diagnostics")
    else:
        if value["level"] == "unknown":
            bad("scored_consistency", "/level", "Scored relevance needs a known level")
        if not math.isclose(sum(r["contribution"] for r in value["reasons"]), score, abs_tol=1e-6):
            bad("contribution_sum", "/reasons", "Contributions must sum to score")
        d = value["scoring_diagnostics"]
        raw = value["raw_total"]
        if d is None or raw is None:
            bad("scored_consistency", "/scoring_diagnostics", "Scored relevance requires diagnostics")
        else:
            groups = d["groups"]
            if len({g["group"] for g in groups}) != len(groups):
                bad("duplicate_scoring_group", "/scoring_diagnostics/groups", "Groups must be unique")
            if not math.isclose(sum(g["weighted_points"] for g in groups), raw, abs_tol=1e-6) or not math.isclose(min(1, raw / 100), score, abs_tol=1e-6) or d["score_saturated"] != (raw >= 100):
                bad("scoring_arithmetic", "/scoring_diagnostics", "Score and diagnostic totals disagree")
    return issues

@dataclass(frozen=True)
class SnapshotRef:
    event_id: str
    event_version: int
    payload_sha256: str
    received_at: datetime
    source_mode: str
    ingest_id: str
    def to_dict(self):
        value = asdict(self)
        value["received_at"] = parse_timestamp(self.received_at).isoformat()
        return value

@dataclass(frozen=True)
class StoredEvent(SnapshotRef):
    bundle: dict
    validation_issues: tuple[ValidationIssue, ...] = ()
    @property
    def snapshot(self):
        return SnapshotRef(*(getattr(self, key) for key in SnapshotRef.__dataclass_fields__))
    def to_dict(self):
        return {**self.snapshot.to_dict(), "bundle": self.bundle, "validation_issues": [x.to_dict() for x in self.validation_issues]}

@dataclass(frozen=True)
class IngestResult:
    status: str
    snapshot: SnapshotRef
    validation_issues: tuple[ValidationIssue, ...] = ()
    @property
    def issues(self):
        return self.validation_issues
    def to_dict(self):
        return {"status": self.status, "snapshot": self.snapshot.to_dict(), "issues": [x.to_dict() for x in self.validation_issues]}

class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self):
        return datetime.now(timezone.utc)

@dataclass(frozen=True)
class FixtureClock:
    timestamp: str | datetime
    def now(self):
        return parse_timestamp(self.timestamp)
