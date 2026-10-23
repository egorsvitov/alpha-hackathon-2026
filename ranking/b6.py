"""Strict B6 decoder and structural semantic validation (not fact checking)."""
from __future__ import annotations
import json
from pathlib import Path
from datetime import date
from decimal import Decimal
from .contracts import (ValidationIssue, InputError, BundleValidationError,
                        validate_contract, resolve_pointer, pointer_path, parse_timestamp)

class BundleDocument(dict):
    """Bundle payload with transport context excluded from JSON serialization."""
    source_line: int | None = None


DEFAULT_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_LINE_BYTES = 4 * 1024 * 1024

def decode_json(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InputError("Duplicate JSON key", issues=[ValidationIssue("duplicate_json_key", "", "Duplicate JSON key")])
            result[key] = value
        return result
    def constant(_):
        raise InputError("Non-finite JSON constant", issues=[ValidationIssue("non_finite_json", "", "Non-finite JSON constant")])
    def floating(token):
        import math
        value = float(token)
        if not math.isfinite(value):
            return constant(token)
        return value
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise InputError("Invalid JSON", issues=[ValidationIssue("invalid_json", "", "Invalid JSON syntax")]) from exc


def load_bundles(path, *, max_bytes=DEFAULT_MAX_BYTES, max_line_bytes=DEFAULT_MAX_LINE_BYTES):
    path = Path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except OSError as exc:
        raise InputError("Cannot read input file") from exc
    if len(raw) > max_bytes:
        raise InputError("Input exceeds configured size limit", issues=[ValidationIssue("input_too_large", "", "Input exceeds configured size limit")])
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise InputError("Input must be UTF-8") from exc
    if path.suffix.lower() == ".jsonl":
        result = []
        for number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > max_line_bytes:
                raise InputError(f"JSONL line {number} exceeds size limit")
            try:
                value = decode_json(line)
            except InputError as exc:
                raise InputError(f"Invalid JSONL line {number}", issues=[ValidationIssue(i.code, f"/lines/{number}" + i.path, i.message, i.severity) for i in exc.issues]) from exc
            if not isinstance(value, dict):
                raise InputError(f"JSONL line {number} must be an object")
            value = BundleDocument(value)
            value.source_line = number
            result.append(value)
        return result
    if path.suffix.lower() != ".json":
        raise InputError("Input must have .json or .jsonl extension")
    value = decode_json(text)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        for index, bundle in enumerate(value):
            if not isinstance(bundle, dict):
                raise InputError("JSON batch items must be objects", issues=[
                    ValidationIssue("invalid_bundle_item", f"/{index}", "Expected an EventBundle object")])
        return value
    raise InputError("JSON input must be an object or an array of objects")


def validate_bundle(bundle) -> list[ValidationIssue]:
    issues = validate_contract(bundle, "event-bundle")
    if issues:
        return issues
    def add(code, path, message, severity="error"):
        issues.append(ValidationIssue(code, path, message, severity))
    # v0.2's generated schema leaves these invariants to application checks.
    if bundle["event"]["availability"] == "active" and bundle["event"]["quality"] != "insufficient" and not bundle["claims"]:
        add("active_claims_required", "/claims", "An active non-insufficient event requires claims")
    indexes = {}
    for group, key in [("claims", "claim_id"), ("sources", "source_id"), ("evidence", "evidence_id"), ("conflicts", "conflict_id")]:
        indexes[group] = {}
        for i, item in enumerate(bundle[group]):
            if item[key] in indexes[group]:
                add("duplicate_id", f"/{group}/{i}/{key}", f"Duplicate {key}")
            indexes[group][item[key]] = item
    claims, sources, evidence = (indexes[x] for x in ("claims", "sources", "evidence"))
    source_keys = {}
    for i, s in enumerate(bundle["sources"]):
        key = (s["article_id"], s["article_version"])
        metadata = {k: v for k, v in s.items() if k != "source_id"}
        if key in source_keys and source_keys[key] != metadata:
            add("source_version_collision", f"/sources/{i}", "Article version has inconsistent metadata")
        source_keys[key] = metadata
        if ((s["content_kind"] == "synthetic") != s["is_synthetic"] or (s["is_full_original_text"] and s["content_kind"] in {"synthetic", "source_based_paraphrase"})):
            add("source_flags_ambiguous", f"/sources/{i}", "Source flags need producer clarification", "warning")
    for i, e in enumerate(bundle["evidence"]):
        path = f"/evidence/{i}"
        source = sources.get(e["source_id"])
        if source is None:
            add("evidence_source_missing", path + "/source_id", "Evidence source does not exist")
        if e["start"] >= e["end"] or e["end"] - e["start"] != len(e["quote"]):
            add("quote_span_invalid", path, "Quote span must match Unicode code point length")
        if source and e["field"] == "title" and source["title"][e["start"]:e["end"]] != e["quote"]:
            add("title_quote_mismatch", path + "/quote", "Quote differs from source title slice")
        if e["field"] == "text":
            add("original_text_not_checked", path + "/quote", "Original article text is unavailable; quote equality not checked", "warning")
    measurements = set()
    def leaves(obj, path=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key not in {"claim_id", "measurement_id", "entity_id", "evidence_ids", "field_evidence"}:
                    yield from leaves(value, path + pointer_path([key]))
        elif isinstance(obj, list):
            for i, value in enumerate(obj):
                yield from leaves(value, path + f"/{i}")
        elif obj is not None and obj != "" and obj != "unknown":
            yield path
    for i, claim in enumerate(bundle["claims"]):
        path = f"/claims/{i}"
        for eid in claim["evidence_ids"]:
            if eid not in evidence:
                add("claim_evidence_missing", path + "/evidence_ids", "Claim evidence does not exist")
        source_ids = {evidence[e]["source_id"] for e in claim["evidence_ids"] if e in evidence}
        if len(source_ids) > 1:
            add("claim_cross_source_evidence", path + "/evidence_ids", "A SourceClaim must cite only one source version")
        valid_paths = []
        for field, ids in claim["field_evidence"].items():
            try:
                resolve_pointer(claim, field)
                valid_paths.append(field)
            except (ValueError, IndexError, KeyError, TypeError):
                add("field_pointer_invalid", path + "/field_evidence" + pointer_path([field]), "Pointer does not resolve")
            if not set(ids) <= set(claim["evidence_ids"]) or any(e not in evidence for e in ids):
                add("field_evidence_missing", path + "/field_evidence" + pointer_path([field]), "Field evidence must belong to claim")
        for field in leaves(claim):
            if not any(field == p or field.startswith(p + "/") for p in valid_paths):
                add("field_evidence_uncovered", path + field, "Extracted field lacks structural evidence coverage")
        periods = [(path + "/time/" + k, v) for k, v in claim["time"].items()]
        for j, m in enumerate(claim["measurements"]):
            mpath = path + f"/measurements/{j}"
            if m["measurement_id"] in measurements:
                add("duplicate_measurement_id", mpath, "Measurement ID must be bundle-unique")
            measurements.add(m["measurement_id"])
            if isinstance(m["value"], dict) and Decimal(m["value"]["min"]) > Decimal(m["value"]["max"]):
                add("measurement_range_invalid", mpath + "/value", "Decimal range is reversed")
            periods.append((mpath + "/comparison_period", m["comparison_period"]))
        for ppath, period in periods:
            if period and period["start"] and period["end"] and date.fromisoformat(period["start"]) > date.fromisoformat(period["end"]):
                add("time_range_invalid", ppath, "Date interval is reversed")
    published = [parse_timestamp(s["published_at"]) for s in sources.values() if s["published_at"] is not None]
    first = bundle["event"]["first_published_at"]
    if (first is None) != (not published) or (published and first is not None and parse_timestamp(first) != min(published)):
        add("first_published_at_mismatch", "/event/first_published_at", "Must equal earliest known current-source publication")
    if published and parse_timestamp(bundle["event"]["updated_at"]) < max(published):
        add("timestamp_consistency", "/event/updated_at", "Producer timestamp precedes a current source publication", "warning")
    for i, conflict in enumerate(bundle["conflicts"]):
        seen = set()
        for j, variant in enumerate(conflict["variants"]):
            key = (variant["claim_id"], variant["field_path"])
            path = f"/conflicts/{i}/variants/{j}"
            if key in seen:
                add("duplicate_conflict_variant", path, "Conflict variant repeated")
            seen.add(key)
            try:
                resolve_pointer(claims[variant["claim_id"]], variant["field_path"])
            except (KeyError, IndexError, ValueError, TypeError):
                add("conflict_reference_invalid", path, "Conflict claim/field does not exist")
    for i, relation in enumerate(bundle["relations"]):
        path = f"/relations/{i}"
        expected_kind = "event" if relation["type"] == "related_stage" else "claim"
        if relation["target_kind"] != expected_kind:
            add("relation_kind_invalid", path + "/target_kind", "Relation type and target kind disagree")
        if relation["from_id"] == relation["to_id"]:
            add("relation_self_loop", path, "Self-loop is not allowed")
        if relation["target_kind"] == "claim":
            if relation["from_id"] not in claims or (relation["type"] == "duplicate_claim" and relation["to_id"] not in claims):
                add("relation_reference_invalid", path, "Relation must reference current claims")
        elif relation["from_id"] != bundle["event"]["event_id"]:
            add("relation_reference_invalid", path, "Related-stage origin must be current event")
    return issues


def require_valid_bundle(bundle):
    issues = validate_bundle(bundle)
    if getattr(bundle, "source_line", None) is not None:
        issues = [ValidationIssue(i.code, i.path, f"JSONL line {bundle.source_line}: " + i.message, i.severity) for i in issues]
    if any(i.severity == "error" for i in issues):
        raise BundleValidationError(issues)
    return issues
