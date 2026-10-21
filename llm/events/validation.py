"""Contract integrity and exact-text grounding, not factuality or entailment."""

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError

from .models import ArticleInput, ArticleRecord, EventBundle, GoldSet, Issue, SourceClaim, Source, Evidence


class ContractValidationError(ValueError):
    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(f"{x.code} at {x.path}: {x.message}" for x in issues))


def _parse(model, payload):
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ContractValidationError([
            Issue(code="invalid_type_or_value", path="/" + "/".join(map(str, e["loc"])), message=e["msg"])
            for e in exc.errors()
        ]) from exc


def pointer(value, path):
    """Resolve a strict RFC 6901 pointer; array negative indices are forbidden."""
    if not path.startswith("/") or re.search(r"~(?![01])", path):
        raise ValueError("Invalid JSON Pointer")
    for part in path[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part):
                raise ValueError("Invalid array index")
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise ValueError("Pointer traverses scalar")
    return value


def article_sha256(article: ArticleInput) -> str:
    """Hash the complete canonical record, including publication metadata."""
    raw = json.dumps(article.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _leaves(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"claim_id", "measurement_id", "entity_id", "evidence_ids", "field_evidence"}:
                continue
            yield from _leaves(child, path + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _leaves(child, path + f"/{i}")
    elif value is not None:
        yield path


def _covers(parent, child):
    return child == parent or child.startswith(parent + "/")


def claim_evidence_issues(claims, sources, evidence, *, check_ids=True):
    """Shared B1/B4 structural checks; inputs are already typed JSON dictionaries."""
    data = {"claims": claims, "sources": sources, "evidence": evidence}
    indexes = {"sources": {s["source_id"]: s for s in sources},
               "evidence": {e["evidence_id"]: e for e in evidence}}
    issues = []
    def fail(code, path, message):
        issues.append(Issue(code=code, path=path, message=message))
    if check_ids:
        for group, key in [("claims", "claim_id"), ("sources", "source_id"), ("evidence", "evidence_id")]:
            seen = set()
            for i, item in enumerate(data[group]):
                if item[key] in seen:
                    fail("duplicate_id", f"/{group}/{i}/{key}", item[key])
                seen.add(item[key])
    measurements = set()
    source_versions = set()
    for i, source in enumerate(data["sources"]):
        key = (source["article_id"], source["article_version"])
        if key in source_versions:
            fail("duplicate_source_version", f"/sources/{i}", str(key))
        source_versions.add(key)
        if source["content_kind"] == "synthetic" and not source["is_synthetic"]:
            fail("invalid_provenance", f"/sources/{i}/is_synthetic", "Synthetic content must be marked")
        if source["is_full_original_text"] and source["content_kind"] in {"synthetic", "source_based_paraphrase"}:
            fail("invalid_provenance", f"/sources/{i}/is_full_original_text", "Paraphrase/synthetic is not a full original")
    for i, evidence in enumerate(data["evidence"]):
        if evidence["source_id"] not in indexes["sources"]:
            fail("missing_source", f"/evidence/{i}/source_id", evidence["source_id"])
        if evidence["end"] - evidence["start"] != len(evidence["quote"]):
            fail("invalid_span", f"/evidence/{i}", "Span length must equal quote code points")
    for i, claim in enumerate(data["claims"]):
        base = f"/claims/{i}"
        if len(set(claim["evidence_ids"])) != len(claim["evidence_ids"]):
            fail("duplicate_reference", base + "/evidence_ids", "Evidence IDs must be unique")
        sources = set()
        for eid in claim["evidence_ids"]:
            if eid not in indexes["evidence"]:
                fail("missing_evidence", base + "/evidence_ids", eid)
            else:
                sources.add(indexes["evidence"][eid]["source_id"])
        if len(sources) > 1:
            fail("multiple_claim_sources", base, "One SourceClaim must belong to one article version")
        for path, eids in claim["field_evidence"].items():
            try:
                pointer(claim, path)
                if path.split("/")[1] not in {"text", "modality", "polarity", "stage", "attribution", "participants", "action", "object", "geographies", "time", "measurements", "scenario"}:
                    raise ValueError("Technical fields cannot serve as semantic evidence paths")
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                fail("invalid_pointer", base + "/field_evidence", f"{path}: {exc}")
            if len(set(eids)) != len(eids) or not set(eids).issubset(claim["evidence_ids"]):
                fail("invalid_field_evidence", base + "/field_evidence", path)
        for path in _leaves(claim):
            if not any(_covers(parent, path) for parent in claim["field_evidence"]):
                fail("uncovered_field", base + path, "Substantive field has no evidence mapping")
        for j, measurement in enumerate(claim["measurements"]):
            mpath = base + f"/measurements/{j}"
            if measurement["measurement_id"] in measurements:
                fail("duplicate_id", mpath + "/measurement_id", measurement["measurement_id"])
            measurements.add(measurement["measurement_id"])
            value = measurement["value"]
            if isinstance(value, dict):
                if Decimal(value["min"]) > Decimal(value["max"]):
                    fail("reversed_range", mpath + "/value", "min > max")
                if measurement["qualifier"] not in {"exact", "approximate"}:
                    fail("invalid_range_qualifier", mpath + "/qualifier", measurement["qualifier"])
        periods = [(base + "/time/" + k, p) for k, p in claim["time"].items()]
        periods += [(base + f"/measurements/{j}/comparison_period", m["comparison_period"]) for j, m in enumerate(claim["measurements"])]
        for path, period in periods:
            if period and period["start"] and period["end"] and period["start"] > period["end"]:
                fail("reversed_period", path, "start > end")
    return issues


def validate_bundle(payload) -> EventBundle:
    bundle = _parse(EventBundle, payload)
    data = bundle.model_dump(mode="json")
    issues = []

    def fail(code, path, message):
        issues.append(Issue(code=code, path=path, message=message))

    indexes = {}
    for group, key in [("claims", "claim_id"), ("sources", "source_id"), ("evidence", "evidence_id"), ("conflicts", "conflict_id")]:
        index = {}
        for i, item in enumerate(data[group]):
            if item[key] in index:
                fail("duplicate_id", f"/{group}/{i}/{key}", item[key])
            index[item[key]] = item
        indexes[group] = index
    issues.extend(claim_evidence_issues(data["claims"], data["sources"], data["evidence"], check_ids=False))
    for i, conflict in enumerate(data["conflicts"]):
        values, scenarios, seen, measurement_scopes = [], set(), set(), set()
        for j, variant in enumerate(conflict["variants"]):
            vpath = f"/conflicts/{i}/variants/{j}"
            key = (variant["claim_id"], variant["field_path"])
            if key in seen:
                fail("duplicate_variant", vpath, str(key))
            seen.add(key)
            try:
                claim = indexes["claims"][variant["claim_id"]]
                value = pointer(claim, variant["field_path"])
                values.append(json.dumps(value, sort_keys=True, ensure_ascii=False))
                match = re.match(r"^/measurements/(0|[1-9][0-9]*)(?:/|$)", variant["field_path"])
                if match:
                    measurement = claim["measurements"][int(match[1])]
                    measurement_scopes.add(tuple(measurement[k] for k in ("metric", "kind", "unit", "value_role")))
                if claim["scenario"] and claim["scenario"]["kind"] != "unspecified":
                    scenarios.add(claim["scenario"]["kind"])
            except (KeyError, IndexError, ValueError, TypeError):
                fail("invalid_conflict_reference", vpath, str(variant))
        if len(set(values)) < 2:
            fail("identical_conflict_variants", f"/conflicts/{i}", "Need distinct field values")
        if len(scenarios) > 1:
            fail("different_scenarios", f"/conflicts/{i}", "Different scenarios alone cannot establish conflict")
        if len(measurement_scopes) > 1:
            fail("different_measurement_scope", f"/conflicts/{i}", "Different metrics, units, kinds or value roles cannot establish numeric conflict")
    for i, relation in enumerate(data["relations"]):
        path = f"/relations/{i}"
        expected_kind = "event" if relation["type"] == "related_stage" else "claim"
        if relation["target_kind"] != expected_kind:
            fail("invalid_relation_kind", path, expected_kind)
        if relation["from_id"] == relation["to_id"]:
            fail("self_relation", path, relation["from_id"])
        if relation["type"] == "related_stage":
            if relation["from_id"] != data["event"]["event_id"]:
                fail("missing_event", path + "/from_id", relation["from_id"])
        else:
            if relation["from_id"] not in indexes["claims"]:
                fail("missing_claim", path + "/from_id", relation["from_id"])
            if relation["type"] == "duplicate_claim" and relation["to_id"] not in indexes["claims"]:
                fail("missing_claim", path + "/to_id", relation["to_id"])
    for group in [data["ranking_features"], {"limitations": data["limitations"]}]:
        for key, values in group.items():
            if len(values) != len(set(values)):
                fail("duplicate_label", "/" + key, "Values must be unique")
    event = data["event"]
    if event["availability"] == "active" and event["quality"] != "insufficient" and not data["claims"]:
        fail("empty_active_event", "/claims", "Active usable event requires claims")
    if event["quality"] != "complete" and not data["limitations"]:
        fail("missing_limitation", "/limitations", "Explain incomplete data")
    dates = [datetime.fromisoformat(s["published_at"]) for s in data["sources"] if s["published_at"]]
    earliest = datetime.fromisoformat(event["first_published_at"]) if event["first_published_at"] else None
    if (min(dates) if dates else None) != earliest:
        fail("invalid_first_publication", "/event/first_published_at", "Must match earliest known source date")
    if issues:
        raise ContractValidationError(issues)
    return bundle


def grounding_issues(claims, sources, evidences, articles):
    """Exact archived metadata, spans and quoted expressions, without an event."""
    records = {}
    issues = []
    for raw in articles:
        record = _parse(ArticleRecord, raw)
        if record.source_id in records:
            issues.append(Issue(code="duplicate_article_source", path="/articles", message=record.source_id))
        records[record.source_id] = record
    for i, source in enumerate(sources):
        record = records.get(source.source_id)
        if record is None:
            issues.append(Issue(code="missing_article", path=f"/sources/{i}", message=source.source_id))
            continue
        expected = {"article_id": record.article.article_id, "article_version": record.article_version,
                    "title": record.article.title, "publisher": record.article.source,
                    "url": record.article.url, "published_at": record.article.published_at,
                    "content_kind": record.article.content_kind,
                    "is_full_original_text": record.article.is_full_original_text,
                    "is_synthetic": record.article.is_synthetic}
        for key, value in expected.items():
            if getattr(source, key) != value:
                issues.append(Issue(code="source_version_mismatch", path=f"/sources/{i}/{key}", message="Metadata differs from archived article"))
    for i, evidence in enumerate(evidences):
        record = records.get(evidence.source_id)
        if record is None:
            continue
        text = getattr(record.article, evidence.field)
        if evidence.end > len(text) or text[evidence.start:evidence.end] != evidence.quote:
            issues.append(Issue(code="quote_mismatch", path=f"/evidence/{i}", message="Quote does not match versioned article slice"))
    evidence_by_id = {e.evidence_id: e for e in evidences}
    for i, claim in enumerate(claims):
        raw = claim.model_dump(mode="json")
        expressions = [(f"/time/{key}/text", p.text) for key, p in claim.time if p is not None]
        expressions += [(f"/measurements/{j}/comparison_period/text", m.comparison_period.text) for j, m in enumerate(claim.measurements) if m.comparison_period]
        if claim.scenario:
            expressions.append(("/scenario/text", claim.scenario.text))
        for path, expression in expressions:
            eids = {eid for parent, ids in raw["field_evidence"].items() if _covers(parent, path) for eid in ids}
            if not any(expression in evidence_by_id[eid].quote for eid in eids):
                issues.append(Issue(code="expression_not_quoted", path=f"/claims/{i}{path}", message=expression))
    return issues


def validate_claim_collection(claims, sources, evidence, articles):
    claims = [_parse(SourceClaim, c) for c in claims]
    sources = [_parse(Source, s) for s in sources]
    evidence = [_parse(Evidence, e) for e in evidence]
    issues = claim_evidence_issues([c.model_dump(mode="json") for c in claims],
        [s.model_dump(mode="json") for s in sources], [e.model_dump(mode="json") for e in evidence])
    if issues:
        raise ContractValidationError(issues)
    issues = grounding_issues(claims, sources, evidence, articles)
    if issues:
        raise ContractValidationError(issues)
    return claims, sources, evidence


def validate_grounding(bundle, articles: list[ArticleRecord]) -> EventBundle:
    bundle = validate_bundle(bundle)
    issues = grounding_issues(bundle.claims, bundle.sources, bundle.evidence, articles)
    if issues:
        raise ContractValidationError(issues)
    return bundle


def validate_gold(payload, corpus=None) -> GoldSet:
    gold = _parse(GoldSet, payload)
    issues = []
    records = {r.article.article_id: r for r in gold.articles}
    if len(records) != len(gold.articles):
        issues.append(Issue(code="duplicate_article", path="/articles", message="Duplicate article ID"))
    claims = {}
    memberships = {}
    event_ids = set()
    for i, bundle in enumerate(gold.bundles):
        if bundle.event.event_id in event_ids:
            issues.append(Issue(code="duplicate_event", path=f"/bundles/{i}", message=bundle.event.event_id))
        event_ids.add(bundle.event.event_id)
        validate_grounding(bundle, gold.articles)
        for claim in bundle.claims:
            if claim.claim_id in claims:
                issues.append(Issue(code="duplicate_claim", path=f"/bundles/{i}", message=claim.claim_id))
            claims[claim.claim_id] = claim
            memberships[claim.claim_id] = bundle.event.event_id
    annotations = {a.article_id: a for a in gold.annotations}
    if len(annotations) != len(gold.annotations) or set(annotations) != set(records):
        issues.append(Issue(code="annotation_coverage", path="/annotations", message="Exactly one annotation per article is required"))
    annotated = set()
    actual_corpus = None
    if corpus is not None:
        parsed = [_parse(ArticleInput, a) for a in corpus]
        actual_corpus = {a.article_id: a for a in parsed}
        if len(actual_corpus) != len(parsed) or set(actual_corpus) != set(records):
            issues.append(Issue(code="corpus_coverage", path="/articles", message="Corpus and gold article IDs differ"))
    for i, annotation in enumerate(gold.annotations):
        record = records.get(annotation.article_id)
        if not record:
            continue
        if annotation.article_version != record.article_version or annotation.article_sha256 != article_sha256(record.article):
            issues.append(Issue(code="article_hash_mismatch", path=f"/annotations/{i}", message=annotation.article_id))
        if actual_corpus is not None and (annotation.article_id not in actual_corpus or article_sha256(actual_corpus[annotation.article_id]) != annotation.article_sha256):
            issues.append(Issue(code="corpus_hash_mismatch", path=f"/annotations/{i}", message=annotation.article_id))
        article_text = record.article.title + "\n" + record.article.text
        for fragment in annotation.excluded:
            if fragment.quote not in article_text:
                issues.append(Issue(code="excluded_quote_missing", path=f"/annotations/{i}/excluded", message=fragment.quote))
        for item in annotation.claims:
            if item.claim_id in annotated or item.claim_id not in claims:
                issues.append(Issue(code="invalid_annotation_claim", path=f"/annotations/{i}/claims", message=item.claim_id))
            annotated.add(item.claim_id)
            if item.expected_event_id is not None and memberships.get(item.claim_id) != item.expected_event_id:
                issues.append(Issue(code="event_membership_mismatch", path=f"/annotations/{i}/claims", message=item.claim_id))
            if item.claim_id in claims:
                sources = {e.source_id for b in gold.bundles for e in b.evidence if e.evidence_id in claims[item.claim_id].evidence_ids}
                if sources != {record.source_id}:
                    issues.append(Issue(code="annotation_source_mismatch", path=f"/annotations/{i}/claims", message=item.claim_id))
                types = {b.event.event_type for b in gold.bundles if any(c.claim_id == item.claim_id for c in b.claims)}
                if types != {item.event_type}:
                    issues.append(Issue(code="annotation_type_mismatch", path=f"/annotations/{i}/claims", message=item.claim_id))
    if annotated != set(claims):
        issues.append(Issue(code="claim_coverage", path="/annotations", message="Every claim must be annotated exactly once"))
    for i, link in enumerate(gold.expected_links):
        if link.from_claim_id not in claims or link.to_claim_id not in claims or link.from_claim_id == link.to_claim_id:
            issues.append(Issue(code="invalid_expected_link", path=f"/expected_links/{i}", message="Missing or identical claims"))
        elif link.relation in {"same_event", "duplicate_claim", "conflicts_with"} and memberships[link.from_claim_id] != memberships[link.to_claim_id]:
            issues.append(Issue(code="link_membership_mismatch", path=f"/expected_links/{i}", message=link.relation))
        elif link.relation == "different_event" and memberships[link.from_claim_id] == memberships[link.to_claim_id]:
            issues.append(Issue(code="link_membership_mismatch", path=f"/expected_links/{i}", message=link.relation))
    if issues:
        raise ContractValidationError(issues)
    return gold
