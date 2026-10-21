from typing import Any
from app.schemas.base import (
    FeedItem,
    NewsEvent,
    Relevance,
    RelevanceFactor,
    Role,
    SignificanceLevel,
)


class UnsupportedContractVersionError(Exception):
    pass


def map_ranked_item_to_feed_item(
    ranked_item: dict[str, Any],
    profile_version: str,
    default_has_bank_position: bool = False,
) -> FeedItem:
    event_data = ranked_item.get("event", {})
    contract_version = event_data.get("contract_version") or event_data.get("version")

    if contract_version and str(contract_version).startswith("0.2"):
        raise UnsupportedContractVersionError(
            f"Event {event_data.get('event_id')} uses B6 v0.2 contract, but ranker currently accepts only B6 v0.1"
        )

    score_val = float(ranked_item.get("score", 0.0))
    if score_val > 1.0:
        score_val = round(score_val / 100.0, 4)
    else:
        score_val = round(score_val, 4)

    level: SignificanceLevel = "high" if score_val >= 0.5 else ("medium" if score_val >= 0.25 else "low")

    reasons = ranked_item.get("reasons", [])
    factors: list[RelevanceFactor] = []
    text_reasons: list[str] = []

    features_used = ranked_item.get("features_used", {})

    for r in reasons:
        group = r.get("group", "general")
        contrib = float(r.get("contribution", 0.0))
        text = r.get("text", "")
        feature_ids = r.get("feature_ids", [])
        event_fields = r.get("event_fields", [])
        basis = r.get("basis")

        prov = {f_id: features_used.get(f_id) for f_id in feature_ids if f_id in features_used}

        factors.append(
            RelevanceFactor(
                feature_key=group,
                label=text.split(";")[0] if ";" in text else text[:40],
                contribution=contrib if contrib <= 1.0 else round(contrib / 100.0, 4),
                detail=text,
                feature_ids=feature_ids,
                event_fields=event_fields,
                basis=basis,
                provenance=prov or None,
            )
        )
        text_reasons.append(text)

    explanation = (
        text_reasons[0]
        if text_reasons
        else (event_data.get("title", "") or "Отраслевой контекст")
    )

    relevance = Relevance(
        score=score_val,
        level=level,
        factors=factors,
        explanation=explanation,
        reasons=text_reasons or None,
        candidate_channels=ranked_item.get("candidate_channels", []),
        missing_context=ranked_item.get("missing_context", []),
        evidence_status=ranked_item.get("evidence_status"),
    )

    topic = event_data.get("event_type") or event_data.get("topic") or "other"

    news_event = NewsEvent(
        event_id=event_data.get("event_id", "unknown"),
        title=event_data.get("title", ""),
        date=event_data.get("published_at") or event_data.get("date") or "2026-10-21",
        topic=topic if topic in ["rates", "sanctions", "fx", "trade", "inflation", "counterparty", "industry", "other"] else "other",
        topic_label=event_data.get("topic_label", "Новости"),
        sources_count=event_data.get("sources_count", 1),
        dedup_note=event_data.get("dedup_note"),
        is_synthetic=event_data.get("synthetic", event_data.get("is_synthetic", True)),
        user_uploaded=event_data.get("user_uploaded"),
        synthetic_link=event_data.get("synthetic_link"),
    )

    return FeedItem(
        event=news_event,
        relevance=relevance,
        why_short=explanation,
        has_bank_position=default_has_bank_position,
        profile_version=profile_version,
    )


def adapt_ranking_response_to_feed(
    ranking_response: dict[str, Any],
    profile_version: str,
) -> list[FeedItem]:
    feed_items_raw = ranking_response.get("feed", [])
    result: list[FeedItem] = []

    for item in feed_items_raw:
        has_bank = False
        event_obj = item.get("event", {})
        if event_obj.get("bank_claims") or event_obj.get("event_type") in ("rates", "key_rate"):
            has_bank = True

        feed_item = map_ranked_item_to_feed_item(
            ranked_item=item,
            profile_version=profile_version,
            default_has_bank_position=has_bank,
        )
        result.append(feed_item)

    return result
