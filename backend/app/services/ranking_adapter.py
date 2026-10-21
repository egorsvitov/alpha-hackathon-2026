from typing import Any
from app.schemas.base import (
    FeedItem,
    NewsEvent,
    Relevance,
    RelevanceFactor,
    SignificanceLevel,
    Topic,
)

TOPIC_MAPPING: dict[str, tuple[Topic, str]] = {
    "policy_rate": ("rates", "Ставки и кредит"),
    "rates": ("rates", "Ставки и кредит"),
    "restriction": ("sanctions", "Ограничения и торговля"),
    "sanctions": ("sanctions", "Ограничения и торговля"),
    "fx": ("fx", "Валюта"),
    "fuel": ("trade", "Топливо и сырье"),
    "trade": ("trade", "Пошлины и экспорт"),
    "wages": ("inflation", "Рынок труда и издержки"),
    "inflation": ("inflation", "Инфляция и спрос"),
    "counterparty": ("counterparty", "Контрагенты"),
    "steel": ("industry", "Металлургия"),
    "industry": ("industry", "Отраслевые события"),
}


class UnsupportedContractVersionError(Exception):
    pass


def map_ranking_result_to_feed_item(
    feed_item_raw: dict[str, Any],
    stored_events_by_key: dict[tuple[str, int, str], dict[str, Any]],
    profile: dict[str, Any],
    profile_version: str,
) -> FeedItem:
    snapshot = feed_item_raw.get("snapshot", {})
    relevance_raw = feed_item_raw.get("relevance", {})

    event_id = snapshot.get("event_id")
    event_version = snapshot.get("event_version")
    payload_sha256 = snapshot.get("payload_sha256")

    lookup_key = (event_id, event_version, payload_sha256)
    stored_event = stored_events_by_key.get(lookup_key)

    if not stored_event:
        for k, v in stored_events_by_key.items():
            if k[0] == event_id and k[1] == event_version:
                stored_event = v
                break

    if not stored_event:
        raise ValueError(
            f"StoredEvent not found for snapshot: event_id={event_id}, version={event_version}, sha={payload_sha256}"
        )

    bundle = stored_event.get("bundle", {})
    schema_version = bundle.get("schema_version")
    if schema_version and str(schema_version).startswith("0.2"):
        raise UnsupportedContractVersionError(
            f"Event {event_id} uses B6 v0.2 contract, but ranker currently supports B6 v0.1"
        )

    event_meta = bundle.get("event", {})
    title = event_meta.get("title") or "Событие"
    first_published_at = event_meta.get("first_published_at") or "2026-10-20T10:00:00+03:00"
    raw_event_type = event_meta.get("event_type", "other")
    topic, topic_label = TOPIC_MAPPING.get(raw_event_type, ("other", "Новости"))

    score_val = float(relevance_raw.get("score", 0.0))
    if score_val > 1.0:
        score_val = round(score_val / 100.0, 4)
    else:
        score_val = round(score_val, 4)

    raw_level = relevance_raw.get("level")
    if raw_level in ("high", "medium", "low"):
        level: SignificanceLevel = raw_level
    else:
        level = "high" if score_val >= 0.5 else ("medium" if score_val >= 0.25 else "low")

    reasons_raw = relevance_raw.get("reasons", [])
    factors: list[RelevanceFactor] = []
    text_reasons: list[str] = []

    profile_features = profile.get("features", {})

    for r in reasons_raw:
        group = r.get("group", "general")
        contrib = round(float(r.get("contribution", 0.0)), 4)
        text = r.get("text", "")
        feature_ids = r.get("feature_ids", [])
        event_paths = r.get("event_paths", [])
        basis = r.get("basis")

        prov = {f_id: profile_features.get(f_id) for f_id in feature_ids if f_id in profile_features}

        label_part = text.split(";")[0] if ";" in text else text[:40]
        factors.append(
            RelevanceFactor(
                feature_key=group,
                label=label_part,
                contribution=contrib,
                detail=text,
                feature_ids=feature_ids,
                event_fields=event_paths,
                basis=basis,
                provenance=prov or None,
            )
        )
        text_reasons.append(text)

    explanation = text_reasons[0] if text_reasons else title

    relevance = Relevance(
        score=score_val,
        level=level,
        factors=factors,
        explanation=explanation,
        reasons=text_reasons or None,
        candidate_channels=relevance_raw.get("candidate_channels", []),
        missing_context=relevance_raw.get("missing_fields", []),
        evidence_status=relevance_raw.get("evidence_status"),
    )

    sources_count = len(bundle.get("sources", [])) or 1

    news_event = NewsEvent(
        event_id=event_id,
        title=title,
        date=first_published_at,
        topic=topic,
        topic_label=topic_label,
        sources_count=sources_count,
        dedup_note=None,
        is_synthetic=True,
    )

    has_bank = False
    claims = bundle.get("claims", [])
    if any(c.get("publisher_type") == "bank" or "bank" in c.get("claim_id", "") for c in claims):
        has_bank = True

    return FeedItem(
        event=news_event,
        relevance=relevance,
        why_short=explanation,
        has_bank_position=has_bank,
        profile_version=profile_version,
    )


def adapt_ranking_response_to_feed(
    ranking_response: dict[str, Any],
    stored_events: list[dict[str, Any]],
    profile: dict[str, Any],
    profile_version: str,
) -> list[FeedItem]:
    stored_by_key = {
        (e["event_id"], e["event_version"], e["payload_sha256"]): e
        for e in stored_events
    }

    feed_items_raw = ranking_response.get("feed", [])
    result: list[FeedItem] = []

    for item in feed_items_raw:
        feed_item = map_ranking_result_to_feed_item(
            feed_item_raw=item,
            stored_events_by_key=stored_by_key,
            profile=profile,
            profile_version=profile_version,
        )
        result.append(feed_item)

    return result
