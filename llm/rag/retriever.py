"""One bounded retrieval attempt. Returned candidates never establish bank opinion."""
from collections import Counter
from datetime import datetime, time, timedelta
import math
from zoneinfo import ZoneInfo

from .corpus import Corpus, parse_date
from .query import digest, load_config, query_plan, tokenize


def temporal_rejection(chunk, as_of, config):
    if not chunk["retrieval_eligible"]:
        return "retrieval_excluded"
    if chunk["historical_only"] and (not config["include_historical"] or config["temporal_mode"] == "research_current"):
        return "historical_only"
    published = chunk["published_at"]
    if not published:
        return "unknown_publication_date"
    date = parse_date(published)
    if isinstance(date, datetime):
        return "future_publication" if date > as_of else None
    moscow = ZoneInfo("Europe/Moscow")
    if config["temporal_mode"] == "daily":
        return "future_publication" if date > as_of.astimezone(moscow).date() else None
    available = datetime.combine(date + timedelta(days=1), time(), moscow)
    return "date_only_not_yet_available" if as_of < available else None


def retrieve_evidence(bundle: dict, profile: dict, relevance: dict, *, as_of: datetime,
                      corpus: Corpus, config: dict, attempt: int = 0,
                      retry_reason: str | None = None) -> dict:
    config = {**corpus.config, **config}
    if type(attempt) is not int or attempt not in (0, 1, 2):
        raise ValueError("attempt must be 0, 1 or 2")
    if attempt and (not isinstance(retry_reason, str) or not retry_reason.strip()):
        raise ValueError("retry_reason required for attempt > 0")
    if as_of.tzinfo is None:
        raise ValueError("as_of requires timezone")
    if config["temporal_mode"] not in ("strict_replay", "daily", "research_current"):
        raise ValueError("unsupported temporal_mode")
    for key in ("top_k", "max_chunks_per_document", "max_context_chars"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"{key} must be positive integer")
    if config["mapping"] != corpus.config["mapping"]:
        raise ValueError("query tokenizer mapping differs from pinned corpus index")
    plan = query_plan(bundle, profile, relevance, config, attempt, retry_reason, corpus.version)
    event = bundle["event"]
    result = {"schema_version": "0.1", "status": "unavailable", "event_id": event["event_id"],
              "event_version": event["event_version"], "company_id": profile["company_id"],
              "profile_version": profile["profile_version"], "as_of": as_of.isoformat(),
              "corpus_version": corpus.version, "query_plan": [plan], "hits": [], "limitations": [],
              "diagnostics": {"rejected": [], "admitted": []}}
    if not corpus.available:
        result["limitations"] = ["corpus_unavailable"]
        return result
    queries = [set(tokenize(q, config["mapping"])) for q in plan["queries"]]
    # Concepts from event object queries are anchors; generic added retry words cannot admit a hit.
    anchors = {t for tokens in queries for t in tokens if t.startswith("concept:")}
    candidates = []
    for c in corpus.chunks:
        reason = temporal_rejection(c, as_of, config)
        if not reason and plan["topic_filters"] and not set(c["topics"]) & set(plan["topic_filters"]):
            reason = "topic_filter"
        if reason:
            result["diagnostics"]["rejected"].append({"chunk_id": c["chunk_id"], "reason": reason})
            continue
        bag = corpus.bags[c["chunk_id"]]
        score = 0.0
        matched = []
        for query, terms in zip(plan["queries"], queries):
            common = terms & bag.keys()
            admitted = bool(common & anchors) if anchors else len({t for t in common if not t.isdigit()}) >= 2
            if not admitted:
                continue
            current = 0.0
            for term in common:
                freq = bag[term]
                idf = math.log(1 + (len(corpus.chunks)-corpus.df[term]+0.5)/(corpus.df[term]+0.5))
                k1, b = config["bm25_k1"], config["bm25_b"]
                current += idf * freq * (k1+1)/(freq+k1*(1-b+b*sum(bag.values())/max(1, corpus.avg_length)))
            if current > 0:
                score = max(score, current)
                matched.append(query)
        if score:
            candidates.append((score, c, matched))
            result["diagnostics"]["admitted"].append({"chunk_id": c["chunk_id"], "reason": "subject_concept_match" if anchors else "two_content_terms"})
        else:
            result["diagnostics"]["rejected"].append({"chunk_id": c["chunk_id"], "reason": "insufficient_relevance"})
    counts = Counter()
    total_chars = 0
    for score, c, matched in sorted(candidates, key=lambda row: (-row[0], row[1]["chunk_id"])):
        if len(result["hits"]) >= config["top_k"]:
            break
        if counts[c["document_id"]] >= config["max_chunks_per_document"]:
            continue
        overlap = any(h["document_id"] == c["document_id"] and h["page"] == c["page"] and
                      max(0, min(h["char_end"], c["char_end"])-max(h["char_start"], c["char_start"])) /
                      min(h["char_end"]-h["char_start"], len(c["text"])) >= config["overlap_threshold"] for h in result["hits"])
        if overlap or total_chars + len(c["text"]) > config["max_context_chars"]:
            continue
        fields = ("document_id", "document_sha256", "chunk_id", "page", "char_start", "char_end", "text_sha256", "source_path", "title", "published_at", "date_status", "verification_status")
        hit = {k: c[k] for k in fields}
        synthetic = c.get("source_kind") == "synthetic" or c.get("source_type") == "synthetic"
        hit.update(quote=c["text"], retrieval_score=round(score, 8), matched_queries=matched,
                   limitations=[], is_synthetic=synthetic, provenance_status=corpus.provenance[c["chunk_id"]])
        if synthetic:
            hit["verification_status"] = "synthetic"
            hit["limitations"].append("synthetic_material")
        elif hit["verification_status"] == "pending_review":
            hit["limitations"].append("ocr_pending_manual_review")
        if any(v != "checked" for v in hit["provenance_status"].values()):
            hit["limitations"].append("provenance_incomplete")
        if config["temporal_mode"] == "daily" and len(c["published_at"]) == 10:
            hit["limitations"].append("intraday_availability_unproven")
        if c["historical_only"]:
            hit["limitations"].append("historical_context_only")
        hit["limitations"].append("forecast_horizon_unverified")
        hit["evidence_id"] = "bank:" + digest([c["document_sha256"], c["chunk_id"], c["char_start"], c["char_end"], c["text_sha256"]])
        corpus.verify_hit(hit)
        result["hits"].append(hit)
        counts[c["document_id"]] += 1
        total_chars += len(c["text"])
    result["status"] = "partial" if result["hits"] else "no_bank_evidence"
    if result["hits"] and all(not set(h["limitations"]) - {"forecast_horizon_unverified", "synthetic_material"} for h in result["hits"]):
        result["status"] = "ok"
    if not corpus.chunks:
        result["limitations"].append("empty_corpus")
    elif not result["hits"]:
        result["limitations"].append("insufficient_relevance")
    result["limitations"].append("retrieval_candidates_require_claim_level_review")
    return result
