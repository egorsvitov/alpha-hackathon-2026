"""Checked retrieval-candidate-batch.v1 → AnalystInput; no IO or model calls.

The upstream schema is pinned at retriever 04bde29. Only two observed additive
candidate fields (processing_status, merge_fallback) are accepted explicitly.
Coordinates are Python Unicode code points, start inclusive, end exclusive.
"""
from __future__ import annotations

from collections import defaultdict
import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.analyst.batch_adapter import BatchValidationError
from app.analyst.input_adapter import stable_hash
from app.analyst.schemas import (
    ROLES, AnalysisPolicy, AnalystInput, AnalystRequest, Candidate, CompanyContext,
    EventBundleRef, MatchContext, Origin, ProjectionBundle, VerifiedFact,
)

VERSION = "retrieval-candidate-batch.v1"
ADAPTER_VERSION = "retrieval-analyst-adapter.v1"
SEMANTIC_LIMIT = (
    "Проверены существование ссылок, хэши и точные срезы оригиналов. "
    "Истинность публикаций, смысловая полнота merge и следование пересказа из цитат автоматически не доказаны. "
    "Модальность и полярность исходных цитат не размечены."
)


class RetrievalAnalysisPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_input_tokens: int = Field(default=8000, gt=0)
    max_projection_chars: int = Field(default=24000, gt=0)
    # Новый batch не содержит окно отбора: 0 означает не вводить новое окно.
    news_window_hours: int = Field(default=0, ge=0)
    max_insights: int = Field(default=20, gt=0)
    analysis_policy: AnalysisPolicy = Field(default_factory=AnalysisPolicy)


@dataclass
class AnalystInputAndDiagnostics:
    analyst_input: AnalystInput
    status: str
    stop_reason: str
    deferred: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    schema_checked: bool = True

    def diagnostics(self) -> dict[str, Any]:
        return dict(adapter_version=ADAPTER_VERSION, status=self.status,
                    stop_reason=self.stop_reason, deferred=self.deferred,
                    candidates=self.candidates, notes=self.notes,
                    semantic_validation="references_and_exact_slices_only")


def _error(code: str) -> None:
    raise BatchValidationError([code])


def _require(condition: bool, code: str) -> None:
    if not condition:
        _error(code)


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _instant(value: Any, path: str) -> datetime:
    try:
        _require(isinstance(value, str) and "T" in value, path + ": invalid_timestamp")
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(dt.tzinfo is not None and dt.utcoffset() is not None, path + ": timezone_required")
        return dt
    except (TypeError, ValueError):
        _error(path + ": invalid_timestamp")


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads((Path(__file__).parent / "contracts/RetrievalCandidateBatch.v1.schema.json").read_text())
    # Local merge-v2 artifacts predate publication of these additive schema fields.
    schema["$defs"]["Candidate"]["properties"].update({
        "processing_status": {"enum": ["ready", "merge_fallback", "partial", "deferred", "unresolved", "failed"]},
        "merge_fallback": {"type": "array", "items": {"type": "object"}},
    })
    return Draft202012Validator(schema)


def _shape(batch: dict[str, Any]) -> dict[int, list[str]]:
    candidate_errors: dict[int, list[str]] = defaultdict(list)
    fatal = []
    for err in _validator().iter_errors(batch):
        path = list(err.absolute_path)
        # Avoid validator messages containing news text or private profile values.
        code = "/" + "/".join(map(str, path)) + ": schema_" + str(err.validator)
        if len(path) > 1 and path[0] == "candidates" and isinstance(path[1], int):
            candidate_errors[path[1]].append(code)
        else:
            fatal.append(code)
    if fatal:
        raise BatchValidationError(fatal)
    return candidate_errors


def _company(profile: dict[str, Any], batch: dict[str, Any]) -> CompanyContext:
    _require(isinstance(profile, dict), "profile_object_required")
    _require(profile.get("schema_version") in (None, "0.1"), "unsupported_profile_schema_version")
    try:
        json.dumps(profile, allow_nan=False)
    except (TypeError, ValueError):
        _error("profile_must_be_finite_json")
    for key in ("company_id", "profile_version"):
        _require(profile.get(key) == batch[key] and bool(profile.get(key)), "profile_" + key + "_mismatch")
    _require(isinstance(profile.get("features"), dict), "profile_features_required")
    # Normalized company-profile 0.1 versions are content hashes, not opaque tags.
    if profile.get("schema_version") == "0.1":
        semantic = copy.deepcopy(profile)
        semantic.pop("profile_version", None)
        _require(isinstance(semantic.get("source_snapshot"), dict), "profile_source_snapshot_required")
        semantic["source_snapshot"].pop("imported_at", None)
        semantic["features"].pop(batch["company_id"] + ":/source_snapshot/imported_at", None)
        _require(stable_hash(semantic) == batch["profile_version"], "profile_content_hash_mismatch")
        for key in ("imported_at", "exported_at", "data_as_of"):
            value = profile["source_snapshot"].get(key)
            if value is not None:
                _instant(value, "profile_source_snapshot/" + key)
        source_hash = profile["source_snapshot"].get("source_sha256")
        if source_hash is not None:
            _require(isinstance(source_hash, str) and re.fullmatch(r"[a-f0-9]{64}", source_hash) is not None, "invalid_profile_source_hash")
    for key in ("limitations", "missing_fields"):
        _require(isinstance(profile.get(key, []), list) and all(isinstance(v, str) for v in profile.get(key, [])), "invalid_profile_" + key)
    facts, unknowns, seen = [], list(profile.get("limitations", [])), set()
    for fid, item in profile["features"].items():
        _require(isinstance(item, dict), "invalid_profile_feature")
        path = item.get("path")
        _require(isinstance(path, str) and path.startswith("/"), "invalid_feature_path")
        _require(fid == item.get("feature_id") == batch["company_id"] + ":" + path, "feature_company_or_id_mismatch")
        _require(fid not in seen, "duplicate_feature_id")
        seen.add(fid)
        if path in ("/company_id", "/source_snapshot") or path.startswith("/source_snapshot/"):
            continue
        if item.get("quality") != "observed" or item.get("value") is None:
            unknowns.append(path + ": не подтверждено наблюдаемым признаком профиля")
            continue
        origin = item.get("origin")
        _require(isinstance(origin, dict) and isinstance(origin.get("kind"), str) and bool(origin["kind"])
                 and isinstance(origin.get("source_path"), str) and bool(origin["source_path"]), "feature_origin_required")
        description = path
        if path == "/revenue/raw":
            description += ": единицу и период проверять отдельно; не считать рублями за текущий период"
        facts.append(VerifiedFact(feature_id=fid, field=path, value=item["value"], description=description,
                                  origin=Origin(type=origin["kind"], ref=origin["source_path"])))
    unknowns.extend(str(p) + ": отсутствует" for p in profile.get("missing_fields", []))
    return CompanyContext(profile_version=batch["profile_version"], verified_facts=facts,
                          profile_snapshot_ref="sha256:" + stable_hash(profile), unknowns=unknowns,
                          synthetic_demo_profile=profile.get("is_synthetic") is True)


def _interests(active: dict[str, Any], batch: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _require(isinstance(active, dict), "interests_object_required")
    for key in ("company_id", "profile_version", "interest_set_version", "encoder_fingerprint"):
        _require(active.get(key) == batch[key] and bool(active.get(key)), "interests_" + key + "_mismatch")
    _require(isinstance(active.get("interests"), list), "interests_required")
    out = {}
    for interest in active["interests"]:
        _require(isinstance(interest, dict), "invalid_interest")
        iid = interest.get("interest_id")
        _require(isinstance(iid, str) and bool(iid.strip()) and iid not in out, "invalid_or_duplicate_interest_id")
        _require(isinstance(interest.get("text"), str) and bool(interest["text"].strip()), "interest_text_required")
        _require(type(interest.get("active")) is bool, "interest_active_required")
        _require(interest.get("family", "activity") in ("activity", "graph", "other"), "invalid_interest_family")
        for key in ("applicable_when", "applicability_basis"):
            _require(interest.get(key) is None or isinstance(interest[key], str), "invalid_interest_" + key)
        # An allowlist deliberately excludes vectors and arbitrary provenance.
        out[iid] = {k: interest[k] for k in ("interest_id", "text", "active", "family", "applicable_when", "applicability_basis") if k in interest}
    return out


def _sources(batch: dict[str, Any], notes: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for s in batch["sources"]:
        rid = s["revision_id"]
        _require(bool(rid.strip()) and bool(s["article_id"].strip()), "empty_source_id")
        _require(bool(s["text"].strip()), "empty_source_text")
        _require(s.get("text_sha256") == text_hash(s["text"]), "source_text_hash_missing_or_mismatch:" + rid)
        precision, published = s.get("publication_precision", "unknown"), s.get("published_at")
        if precision == "unknown":
            _require(published is None, "unknown_date_must_be_null:" + rid)
        elif precision == "timestamp":
            _instant(published, "published_at:" + rid)
        else:
            try:
                _require(isinstance(published, str) and len(published) == 10, "invalid_day:" + rid)
                date.fromisoformat(published)
            except (ValueError, TypeError):
                _error("invalid_day:" + rid)
        if s.get("observed_at") is not None:
            _instant(s["observed_at"], "observed_at:" + rid)
        if s.get("url") is not None:
            try:
                url = urlsplit(s["url"])
                _require(url.scheme in ("http", "https") and bool(url.hostname) and not url.username
                         and not any(c.isspace() for c in s["url"]), "invalid_source_url:" + rid)
            except ValueError:
                _error("invalid_source_url:" + rid)
        if rid in out:
            _require(out[rid] == s, "conflicting_source_revision:" + rid)
            notes.append("duplicate_source_revision:" + rid)
        out[rid] = s
    return out


def _fragments(item: dict[str, Any], sources: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    refs = item["source_revision_ids"]
    _require(len(refs) == len(set(refs)), "duplicate_source_revision_reference")
    _require(all(r in sources for r in refs), "missing_source_revision")
    if item.get("kind", "article") == "article":
        _require(len(refs) == 1 and not item.get("was_merged", False), "invalid_raw_membership")
        source = sources[refs[0]]
        _require(item["item_id"] == source["revision_id"], "article_revision_id_mismatch")
        _require(item["text"] == source["text"] and item.get("title", "") == source.get("title", ""), "article_source_content_mismatch")
        _require(item.get("publication_count", 1) == 1 and item.get("source_count", 0) <= 1, "invalid_raw_counts")
        proofs = item.get("derived_from", {}).get("fragment_texts", {})
        hashes = item.get("derived_from", {}).get("fragment_text_hashes", [])
        _require(isinstance(proofs, dict) and isinstance(hashes, list) and set(proofs) == set(hashes), "raw_fragment_proof_membership_mismatch")
        for digest, quote in proofs.items():
            _require(isinstance(quote, str) and bool(quote.strip()) and text_hash(quote) == digest
                     and quote in source["text"], "raw_fragment_proof_mismatch")
        parts = []
        for start in range(0, len(source["text"]), 1200):
            end = min(start + 1200, len(source["text"]))
            quote = source["text"][start:end]
            parts.append(dict(id=stable_hash([refs[0], "text", start, end, quote]), article_id=refs[0],
                              field="text", start=start, end=end, text=quote))
        return parts, {}
    derived = item.get("derived_from", {})
    event = derived.get("event")
    _require(isinstance(event, dict), "merged_event_provenance_required")
    for key in ("operation_id", "result_id", "configuration_fingerprint", "cluster_id", "cluster_revision"):
        _require(isinstance(derived.get(key), str) and bool(derived[key]), "missing_merge_" + key)
    for key in ("operation_id", "result_id", "configuration_fingerprint"):
        _require(re.fullmatch(r"[0-9a-f]{64}", derived[key]) is not None, "invalid_merge_hash:" + key)
    _require(event.get("id") == item["item_id"], "event_id_mismatch")
    _require(item.get("item_version") == stable_hash(event), "event_version_hash_mismatch")
    for key in ("title", "text", "embedding_text", "was_merged", "publication_count", "source_count"):
        _require(event.get(key) == item.get(key), "event_item_" + key + "_mismatch")
    _require(event.get("article_ids") == sorted(refs), "event_source_membership_mismatch")
    _require(item["publication_count"] == len(refs), "event_publication_count_mismatch")
    _require(item["source_count"] == len({sources[r].get("source") for r in refs if sources[r].get("source")}), "event_source_count_mismatch")
    _require(item["was_merged"] == (len(refs) > 1), "event_merge_flag_mismatch")
    fs = event.get("fragments")
    _require(isinstance(fs, list) and bool(fs), "event_fragments_required")
    ids = set()
    for f in fs:
        _require(isinstance(f, dict) and set(f) == {"id", "article_id", "field", "start", "end", "text"}, "invalid_fragment_shape")
        rid, field = f["article_id"], f["field"]
        _require(rid in refs and field in ("title", "text"), "fragment_source_reference_missing")
        if field == "title":
            _require(sources[rid].get("title_origin", "source") == "source", "generated_title_is_not_source_evidence")
        start, end = f["start"], f["end"]
        original = sources[rid].get(field, "")
        _require(type(start) is int and type(end) is int and 0 <= start < end <= len(original), "invalid_fragment_coordinates")
        _require(original[start:end] == f["text"], "fragment_quote_mismatch")
        _require(f["id"] == stable_hash([rid, field, start, end, f["text"]]), "fragment_hash_mismatch")
        _require(f["id"] not in ids, "duplicate_fragment_id")
        ids.add(f["id"])
    _require(sorted({f["article_id"] for f in fs}) == sorted(refs), "fragment_membership_mismatch")
    paragraphs, disagreements = event.get("paragraphs"), event.get("disagreements")
    _require(isinstance(paragraphs, list) and bool(paragraphs) and isinstance(disagreements, list), "invalid_merge_paragraphs")
    for p in paragraphs + disagreements:
        _require(isinstance(p, dict) and set(p) == {"text", "fragment_ids"}, "invalid_merge_paragraph_shape")
        links = p["fragment_ids"]
        _require(isinstance(links, list) and bool(links) and all(isinstance(x, str) for x in links), "invalid_paragraph_refs")
        _require(len(links) == len(set(links)) and set(links) <= ids, "broken_paragraph_reference")
        _require(isinstance(p["text"], str) and bool(p["text"].strip()), "invalid_paragraph_text")
    if "title_fragment_ids" in event:
        _require(isinstance(event["title_fragment_ids"], list) and bool(event["title_fragment_ids"])
                 and all(isinstance(x, str) and x in ids for x in event["title_fragment_ids"]), "broken_title_reference")
    _require(event["text"] == "\n\n".join(p["text"] for p in paragraphs + disagreements), "event_text_paragraph_mismatch")
    return fs, dict(origin="derived_merge", independent_evidence=False, semantic_entailment_checked=False,
                    text=event["text"], paragraphs=paragraphs, disagreements=disagreements,
                    title_fragment_ids=event.get("title_fragment_ids", []),
                    operation_id=derived["operation_id"], result_id=derived["result_id"],
                    configuration_fingerprint=derived["configuration_fingerprint"])


def _candidate(c: dict[str, Any], batch: dict[str, Any], sources: dict[str, Any], interests: dict[str, Any]) -> Candidate:
    item = c["item"]
    for key in ("item_id", "item_version"):
        _require(isinstance(item.get(key, "1" if key == "item_version" else None), str)
                 and bool(item.get(key, "1" if key == "item_version" else None)), "invalid_" + key)
    fs, derived = _fragments(item, sources)
    scores = c["interest_scores"]
    _require(bool(scores) and c["best_interest_id"] in scores, "best_interest_reference_missing")
    _require(all(i in interests and interests[i]["active"] for i in scores), "inactive_or_unknown_interest_reference")
    _require(all(math.isfinite(v) and -1.000001 <= v <= 1.000001 for v in [c["embedding_score"], c["selection_score"], *scores.values()]), "invalid_cosine_score")
    refs = item["source_revision_ids"]
    as_of = _instant(batch["as_of"], "as_of")
    for rid in refs:
        s = sources[rid]
        if s.get("publication_precision") == "timestamp":
            _require(_instant(s["published_at"], "published_at") <= as_of, "source_after_as_of")
        elif s.get("publication_precision") == "day":
            # Compare calendar bounds without inventing a publication timezone.
            _require(date.fromisoformat(s["published_at"]) <= as_of.date(), "source_day_after_as_of")
        if s.get("observed_at"):
            _require(_instant(s["observed_at"], "observed_at") <= as_of, "source_observed_after_as_of")
    is_event = item.get("kind", "article") == "event"
    canonical = "event:" + item["item_id"] if is_event else "article:" + sources[refs[0]]["article_id"]
    cid = stable_hash([ADAPTER_VERSION, batch["company_id"], canonical])
    groups = sorted(set(c.get("group_ids", []) + ([item["derived_from"]["cluster_id"]] if is_event else [])))
    _require(all(isinstance(g, str) and bool(g) for g in groups), "invalid_reading_group_id")
    claims, evidence = [], []
    for f in fs:
        evid = "quote:" + f["id"]
        claims.append(dict(claim_id="claim:" + f["id"], text=f["text"], modality="unknown", polarity="unknown",
                           origin="original_quote", evidence_ids=[evid]))
        evidence.append(dict(evidence_id=evid, source_id=f["article_id"], quote=f["text"], field=f["field"],
                             start=f["start"], end=f["end"], origin="original_quote"))
    limitations = [SEMANTIC_LIMIT, *batch.get("limitations", []), *c.get("limitations", [])]
    if c.get("processing_status") == "merge_fallback":
        limitations.append("Merge недоступен или не завершён: анализируется только исходная публикация. Полнота события не установлена.")
    precision_unknown = any(sources[r].get("publication_precision", "unknown") != "timestamp" for r in refs)
    if precision_unknown:
        limitations.append("Дата события не установлена с точностью до момента; даты и точность каждого источника сохранены отдельно.")
    if not is_event:
        limitations.append("Идентичность статьи не доказывает тождество событий в разных статьях; межстатейная дедупликация не выполнена.")
    published = None if precision_unknown else min((sources[r]["published_at"] for r in refs), key=lambda v: _instant(v, "published_at"))
    bundle = ProjectionBundle.model_validate(dict(bundle_schema_version=VERSION,
        event=dict(event_id=canonical, event_version=1, text_version=item.get("item_version", "1"),
                   text_sha256=text_hash(item["text"]), first_published_at=published,
                   title=item.get("title"), title_origin="derived_merge" if is_event else sources[refs[0]].get("title_origin", "unknown"),
                   quality="partial", processing_status=c.get("processing_status", "ready"),
                   retrieval_status=batch["status"], retrieval_stop_reason=batch["stop_reason"]),
        claims=claims, evidence=evidence, derived_context=derived, limitations=limitations,
        conflicts=[dict(origin="derived_merge", description=p["text"],
                        variants=[dict(claim_id="claim:" + fid) for fid in p["fragment_ids"]])
                   for p in derived.get("disagreements", [])],
        sources=[dict(source_id=r, article_id=sources[r]["article_id"], title=sources[r].get("title"),
                      publisher=sources[r].get("source"), published_at=sources[r].get("published_at"),
                      publication_precision=sources[r].get("publication_precision", "unknown"),
                      url=sources[r].get("url"), text_sha256=sources[r]["text_sha256"],
                      title_origin=sources[r].get("title_origin", "unknown")) for r in refs]))
    hypotheses = ["Поисковая гипотеза, не факт компании: " + interests[i]["text"] for i in sorted(scores)]
    return Candidate(candidate_id=cid, cluster_id=canonical, canonical_event_id=canonical, reading_group_ids=groups,
        event_bundle_refs=[EventBundleRef(event_id=canonical, event_version=1, snapshot_sha256=stable_hash(bundle.model_dump(mode="json")),
                                         snapshot_ref="retrieval-item:" + item["item_id"] + "@" + item.get("item_version", "1"))],
        analysis_projection=dict(event_bundles=[bundle]),
        match_context=MatchContext(impact_hypotheses=hypotheses, assumptions_not_verified=[
            "Cosine и inherited_trigger описывают отбор, а не значимость, уверенность или подтвержденную экспозицию компании.",
            *[str(interests[i][k]) for i in sorted(scores) for k in ("applicable_when", "applicability_basis") if interests[i].get(k)],
        ]))


def adapt_retrieval_batch(batch: dict[str, Any], company_profile: dict[str, Any], active_interests: dict[str, Any],
                          role: str, analysis_policy: dict[str, Any] | RetrievalAnalysisPolicy | None = None,
                          *, prompt_version: str | None = None, fewshot: bool = False,
                          reserve_tokens: int = 0) -> AnalystInputAndDiagnostics:
    """Invalid package/context raises BatchValidationError; candidate errors are isolated.

    Oversized candidates are held as context_too_large, never sent to the LLM.
    No dates, source text hashes, company facts or semantic labels are invented.
    """
    try:
        policy = RetrievalAnalysisPolicy.model_validate({} if analysis_policy is None else analysis_policy)
    except ValidationError:
        _error("invalid_analysis_policy")
    errors = _shape(batch)
    _require(role in ROLES, "invalid_role")
    _instant(batch["as_of"], "as_of")
    for key in ("request_id", "company_id", "profile_version", "interest_set_version", "corpus_id", "trace_id", "encoder_fingerprint"):
        _require(bool(batch[key].strip()), "empty_" + key)
    _require(batch["requested_k"] > 0 and batch["index_generation"] >= 0, "invalid_retrieval_limits")
    _require(batch["status"] != "empty" or not batch["candidates"], "empty_batch_has_candidates")
    ctx = _company(company_profile, batch)
    interests = _interests(active_interests, batch)
    notes: list[str] = []
    sources = _sources(batch, notes)
    inp = AnalystInput(request=AnalystRequest(request_id=batch["request_id"], company_id=batch["company_id"], role=role,
        as_of=batch["as_of"], news_window_hours=policy.news_window_hours, max_insights=policy.max_insights),
        company_context=ctx, analysis_policy=policy.analysis_policy)
    result = AnalystInputAndDiagnostics(inp, batch["status"], batch["stop_reason"], deferred=batch.get("deferred", []), notes=notes)
    if result.deferred and result.status == "ready":
        result.status = "partial"
        notes.append("ready_with_deferred: неполная обработка не означает нерелевантность")
    seen: dict[str, tuple[str, Candidate]] = {}
    conflicted: set[str] = set()
    pending: list[tuple[Candidate, dict[str, Any], dict[str, Any]]] = []
    for index, raw in enumerate(batch["candidates"]):
        info: dict[str, Any] = dict(index=index, state="ready")
        result.candidates.append(info)
        try:
            if index in errors:
                raise BatchValidationError(errors[index])
            item = raw["item"]
            info["item_id"] = item["item_id"]
            related = [d for d in result.deferred if d.get("cluster_id") in raw.get("group_ids", [])]
            if related and "processing_status" not in raw and item.get("kind", "article") == "article":
                raw = dict(raw, processing_status="merge_fallback", merge_fallback=related)
            processing = raw.get("processing_status", "ready")
            info.update(processing_status=processing, merge_fallback=raw.get("merge_fallback", []))
            cand = _candidate(raw, batch, sources, interests)
            cand.max_input_tokens = policy.max_input_tokens
            info.update(candidate_id=cand.candidate_id, canonical_event_id=cand.canonical_event_id,
                        reading_group_ids=cand.reading_group_ids)
            if processing in ("deferred", "unresolved", "failed"):
                info["state"] = processing
                continue
            representation = stable_hash(cand.model_dump(mode="json", exclude={"reading_group_ids"}))
            _require(cand.canonical_event_id not in conflicted, "conflicting_event_representations")
            if cand.canonical_event_id in seen:
                old_hash, old = seen[cand.canonical_event_id]
                if old_hash != representation:
                    conflicted.add(cand.canonical_event_id)
                    for diagnostic in result.candidates:
                        if diagnostic.get("canonical_event_id") == cand.canonical_event_id:
                            diagnostic.update(state="invalid", errors=["conflicting_event_representations"])
                    _error("conflicting_event_representations")
                old.reading_group_ids[:] = sorted(set(old.reading_group_ids + cand.reading_group_ids))
                info["state"] = "duplicate"
                continue
            seen[cand.canonical_event_id] = (representation, cand)
            info["state"] = "merge_fallback" if processing == "merge_fallback" else "partial" if processing == "partial" else "ready"
            pending.append((cand, info, raw))
        except BatchValidationError as exc:
            info.update(state="invalid", errors=exc.errors)
        except (KeyError, TypeError, ValueError, OverflowError):
            # Open derived_from is not allowed to crash a batch or disclose text.
            info.update(state="invalid", errors=["malformed_candidate_provenance"])
    from app.analyst.prompt_budget import estimate_prompt_tokens
    for cand, info, raw in pending:
        if cand.canonical_event_id in conflicted:
            continue
        cand.retrieval_context_hash = stable_hash(dict(candidate=cand.model_dump(mode="json"), company=ctx.model_dump(mode="json"),
            interests=[interests[i] for i in sorted(raw["interest_scores"])], interest_set_version=batch["interest_set_version"],
            request=inp.request.model_dump(mode="json", exclude={"request_id"}), policy=policy.model_dump(mode="json"),
            adapter_version=ADAPTER_VERSION))
        projection_chars = len(json.dumps(cand.analysis_projection.model_dump(mode="json"), ensure_ascii=False))
        tokens = estimate_prompt_tokens(inp, cand, prompt_version, fewshot=fewshot)
        info.update(projection_chars=projection_chars, estimated_prompt_tokens=tokens, token_budget=policy.max_input_tokens,
                    reserve_tokens=reserve_tokens)
        if projection_chars > policy.max_projection_chars or tokens + reserve_tokens > policy.max_input_tokens:
            info.update(state="context_too_large", omitted="entire_candidate", omitted_source_ids=raw["item"]["source_revision_ids"],
                        omitted_claim_ids=[c.claim_id for c in cand.analysis_projection.event_bundles[0].claims])
            continue
        inp.candidates.append(cand)
    if any(c["state"] not in ("ready", "duplicate") for c in result.candidates):
        result.status = "partial"
    return result
