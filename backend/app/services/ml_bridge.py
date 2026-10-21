import json
import logging
from pathlib import Path
from typing import Any

from app.schemas.base import Claim, Evidence, Insight, NewsEvent, WhatHappened

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
EVENTS_DIR = REPO_ROOT / "data" / "processed" / "events"


def load_live_event_bundles() -> list[dict[str, Any]]:
    candidate_files = [
        EVENTS_DIR / "bundles.v0.2.jsonl",
        EVENTS_DIR / "active.v0.2.jsonl",
        EVENTS_DIR / "events.jsonl",
        REPO_ROOT / "data" / "examples" / "personalization" / "events.jsonl",
    ]

    for fpath in candidate_files:
        if fpath.exists():
            bundles: list[dict[str, Any]] = []
            try:
                with open(fpath, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            bundles.append(json.loads(line))
                if bundles:
                    logger.info("Loaded %d event bundles from %s", len(bundles), fpath)
                    return bundles
            except Exception as e:
                logger.warning("Failed to parse bundles from %s: %s", fpath, e)

    return []


def bundle_to_news_event(bundle: dict[str, Any]) -> NewsEvent:
    ev = bundle.get("event", {})
    ev_id = ev.get("event_id", "unknown")
    title = ev.get("title") or "Событие"
    date = ev.get("first_published_at") or ev.get("updated_at") or "2026-10-21"
    raw_type = ev.get("event_type", "other")

    sources = bundle.get("sources", [])
    sources_count = len(sources) or 1

    return NewsEvent(
        event_id=ev_id,
        title=title,
        date=date,
        topic="other",
        topic_label=raw_type.replace("_", " ").capitalize(),
        sources_count=sources_count,
        is_synthetic=bundle.get("schema_version") == "0.1",
    )


def bundle_to_claims(bundle: dict[str, Any]) -> list[Claim]:
    claims_raw = bundle.get("claims", [])
    result: list[Claim] = []

    for c in claims_raw:
        text = c.get("text") or c.get("claim_id") or ""
        evidence_ids = c.get("evidence_ids", [])
        kind = "fact" if c.get("polarity") == "affirmed" else "inference"

        result.append(
            Claim(
                text=text,
                kind=kind,
                evidence_ids=evidence_ids,
            )
        )
    return result


def bundle_to_evidence(bundle: dict[str, Any]) -> list[Evidence]:
    sources = bundle.get("sources", [])
    result: list[Evidence] = []

    for s in sources:
        sid = s.get("source_id", "s1")
        title = s.get("title") or s.get("publisher") or "Источник"
        pub = s.get("publisher", "СМИ")
        quote = s.get("quote", "")

        result.append(
            Evidence(
                evidence_id=sid,
                type="news",
                title=title,
                publisher=pub,
                date=s.get("published_at"),
                quote=quote,
                is_synthetic=False,
            )
        )
    return result
