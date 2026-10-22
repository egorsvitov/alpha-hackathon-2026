"""Versioned, compact economic query plans; no external models or services."""
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def load_config(directory=None):
    directory = Path(directory) if directory else Path(__file__).parent / "config"
    policy = json.loads((directory / "retrieval-policy.json").read_text())
    policy["mapping"] = json.loads((directory / "topic-mapping.json").read_text())
    return policy


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")


def tokenize(text, mapping):
    normalized = normalize(text)
    words = re.findall(r"[а-яa-z0-9]+", normalized)
    aliases = {normalize(form): "concept:" + concept
               for concept, forms in mapping["concepts"].items() for form in forms if " " not in form}
    tokens = [aliases.get(w, w) for w in words if w not in mapping["stop_words"]]
    for concept, forms in mapping["concepts"].items():
        if any(" " in form and re.search(r"(?<!\w)" + re.escape(normalize(form)) + r"(?!\w)", normalized)
               for form in forms):
            tokens.append("concept:" + concept)
    return tokens


def query_plan(bundle, profile, relevance, config, attempt, retry_reason, corpus_version):
    mapping = config["mapping"]
    features = bundle.get("ranking_features", {})
    tags = [str(t) for key in ("topics", "objects", "metrics", "factors")
            for t in features.get(key, [])]
    tags.extend(str(c.get("object", "")) for c in bundle.get("claims", []))
    topics = sorted({topic for tag in tags for topic in mapping["topics"].get(tag, [])})
    queries = [mapping["queries"][t] for t in topics if t in mapping["queries"]]
    reasons = ["event_object_topic_mapping"] if queries else ["unknown_topic_lexical_fallback"]
    if not queries:
        queries = [bundle.get("event", {}).get("title", "") + " " + " ".join(tags)]
    # Only mechanisms explicitly present in relevance may expand a query.
    mechanisms = json.dumps(relevance.get("mechanisms", relevance.get("reasons", [])), ensure_ascii=False)
    if "кредит" in normalize(mechanisms) or "credit" in normalize(mechanisms):
        queries.append("кредитование стоимость кредита")
        reasons.append("relevance_credit_mechanism")
    horizons = [c.get("time", {}).get("forecast_horizon") for c in bundle.get("claims", [])]
    horizon_text = [h.get("text", "") for h in horizons if isinstance(h, dict)]
    if horizon_text:
        queries = [q + " " + " ".join(horizon_text) for q in queries]
    filters = topics
    if attempt == 1:
        filters = []
        queries = [q + " аналитика ожидания" for q in queries]
        reasons.append("retry_remove_topic_filter")
    if attempt == 2:
        filters = []
        queries = [q + " перспективы динамика причины" for q in queries]
        reasons.append("retry_object_dynamics_context")
    return {"attempt": attempt, "queries": list(dict.fromkeys(queries)), "topic_filters": filters,
            "temporal_mode": config["temporal_mode"], "retry_reason": retry_reason,
            "config_version": digest(config), "corpus_version": corpus_version, "reasons": reasons}
