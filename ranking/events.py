"""Validated structured events; this module does not extract facts from raw news."""
from datetime import datetime, timezone
import json
from pathlib import Path


class EventError(ValueError):
    pass


def timestamp(value):
    if not isinstance(value, str):
        raise EventError("Время должно быть ISO-8601 строкой с часовым поясом")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventError(f"Некорректная дата: {value}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EventError(f"Нужен часовой пояс: {value}")
    return parsed.astimezone(timezone.utc)


LIST_FIELDS = ["industries", "okved_prefixes", "regions", "countries", "entity_ids"]


def validate_event(raw):
    if not isinstance(raw, dict):
        raise EventError("Событие должно быть объектом")
    result = dict(raw)
    for key in ["event_id", "title", "text", "event_type", "source_id"]:
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise EventError(f"Нужно непустое строковое поле {key}")
    result.setdefault("cluster_id", result["event_id"])
    if not isinstance(result["cluster_id"], str) or not result["cluster_id"].strip():
        raise EventError("cluster_id должен быть непустой строкой")
    result.setdefault("version", 1)
    if type(result["version"]) is not int or result["version"] < 1:
        raise EventError("version должен быть положительным целым")
    if type(result.get("synthetic")) is not bool:
        raise EventError("Нужен явный boolean synthetic")
    for key in LIST_FIELDS:
        result.setdefault(key, [])
        if not isinstance(result[key], list) or any(not isinstance(s, str) or not s.strip() for s in result[key]):
            raise EventError(f"{key} должен быть списком непустых строк")
        result[key] = sorted(set(result[key]))
    for key in ["published_at", "effective_at", "observed_at"]:
        result.setdefault(key, None)
        if result[key] is not None:
            timestamp(result[key])
    result.setdefault("entity_resolution", "unresolved")
    if result["entity_resolution"] not in {"unresolved", "synthetic", "verified"}:
        raise EventError("Неизвестный статус entity_resolution")
    if result["entity_resolution"] == "synthetic" and not result["synthetic"]:
        raise EventError("Синтетическое сопоставление нельзя выдавать за реальное")
    return result


def validate_events(values):
    if not isinstance(values, list):
        raise EventError("Нужен список событий либо объект с полем events")
    result, seen = [], {}
    for raw in values:
        event = validate_event(raw)
        key = (event["event_id"], event["version"])
        if key in seen:
            if seen[key] != event:
                raise EventError(f"Конфликт одной версии события: {key}")
            continue
        seen[key] = event
        result.append(event)
    return result


def load_events(path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_events(document.get("events") if isinstance(document, dict) else document)


def available_versions(events, as_of, lookback_days=30):
    """Select latest observable cluster versions BEFORE scoring (no future leakage)."""
    now, clusters, excluded = timestamp(as_of), {}, []
    for event in events:
        reason = None
        if event["published_at"] is None:
            reason = "unknown_publication_time"
        else:
            published = timestamp(event["published_at"])
            upcoming = event["effective_at"] and 0 <= (timestamp(event["effective_at"])-now).total_seconds() <= 14*86400
            if published > now:
                reason = "not_yet_published"
            elif event.get("observed_at") and timestamp(event["observed_at"]) > now:
                reason = "not_yet_observed"
            elif (now-published).total_seconds() > lookback_days*86400 and not upcoming:
                reason = "outside_lookback_window"
        if reason:
            excluded.append({"event_id": event["event_id"], "reason": reason})
        else:
            clusters.setdefault(event["cluster_id"], []).append(event)
    result = []
    for items in clusters.values():
        # A late reprint of an old version must not overwrite a material update.
        # The upstream clusterer owns monotonic material-version assignment.
        latest = max(items, key=lambda e: (e["version"], timestamp(e["published_at"]), e["event_id"]))
        result.append(latest)
        excluded.extend({"event_id": e["event_id"], "reason": "superseded_or_duplicate_cluster"}
                        for e in items if e is not latest)
    return sorted(result, key=lambda e: e["event_id"]), excluded
