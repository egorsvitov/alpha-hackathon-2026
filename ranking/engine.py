"""Candidate channels, transparent pair scoring and diverse event feeds."""
import json
import hashlib
import math
from pathlib import Path
from .events import available_versions, timestamp, validate_event, validate_events
from .lexical import activity_overlap

RULES_PATH = Path(__file__).parent / "config/rules.json"


def load_rules(path=RULES_PATH):
    rules = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"version", "lookback_days", "min_feed_score", "max_per_topic", "priority_override_score",
                "weights", "product_rules", "mechanism_rules", "mechanism_missing_fields"}
    if not isinstance(rules, dict) or not required <= rules.keys():
        raise ValueError("Неполная конфигурация правил")
    for key in ["lookback_days", "max_per_topic"]:
        if type(rules[key]) is not int or rules[key] < 1:
            raise ValueError(f"Некорректное положительное целое {key}")
    for key in ["min_feed_score", "priority_override_score"]:
        if not isinstance(rules[key], (int, float)) or not math.isfinite(rules[key]) or not 0 <= rules[key] <= 100:
            raise ValueError(f"Некорректный порог {key}")
    groups = [rules["weights"], *rules["product_rules"].values(), *rules["mechanism_rules"].values()]
    for group in groups:
        if any(type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 100
               for value in group.values()):
            raise ValueError("Веса должны быть конечными неотрицательными числами до 100")
    return rules


def score_event(profile, event, as_of, rules=None):
    rules = rules or load_rules()
    event = validate_event(event)
    now = timestamp(as_of)
    weight, reasons, warnings = rules["weights"], [], []
    uid = profile["company_id"]

    def add(group, amount, text, paths, event_fields, basis="observed_match"):
        if amount <= 0:
            return
        refs = [f"{uid}:{p}" for p in paths]
        if any(ref not in profile["features"] for ref in refs):
            raise ValueError(f"Отсутствует происхождение признака: {refs}")
        reasons.append({"group": group, "contribution": round(amount, 4), "text": text,
                        "feature_ids": refs, "event_fields": event_fields, "basis": basis})

    current, excluded = available_versions([event], as_of, rules["lookback_days"])
    if not current:
        return {"event_id": event["event_id"], "cluster_id": event["cluster_id"],
                "score": 0, "eligible": False, "exclusion_reason": excluded[0]["reason"],
                "reasons": [], "candidate_channels": [], "features_used": {},
                "missing_context": [], "warnings": [], "evidence_status": "insufficient",
                "scoring_version": rules["version"], "event": event}

    code, industry = profile["okved"]["code"], profile["industry"]
    prefix_match = any(code and (code == p or code.startswith(p+".")) for p in event["okved_prefixes"])
    if prefix_match:
        add("industry", weight["okved"], "ОКВЭД компании входит в явно указанный охват события.",
            ["okved.code"], ["okved_prefixes"])
    elif industry and industry in event["industries"]:
        add("industry", weight["industry"], "Совпадает укрупненная отрасль; последствия требуют проверки.",
            ["industry"], ["industries"])
    overlap = activity_overlap(profile, event)
    if overlap:
        add("lexical", weight["lexical"]*min(1, len(overlap)/3),
            "Слова описания деятельности совпадают с текстом события: "+", ".join(overlap)+"; это слабый текстовый сигнал.",
            ["okved.name"], ["title", "text"], "lexical_match_only")
    region_match = profile["region"] and profile["region"].casefold() in {r.casefold() for r in event["regions"]}
    if region_match:
        add("region", weight["region"], "Событие затрагивает регион регистрации; места операций неизвестны.",
            ["region"], ["regions"], "conditional_mechanism")
    if profile.get("fts_present") is not False and set(profile["countries"]) & {c.upper() for c in event["countries"]}:
        add("country", weight["country"], "Страна события есть в сведениях ФТС; направление и доля потока неизвестны.",
            ["countries"], ["countries"], "conditional_mechanism")
    product_rules = rules["product_rules"].get(event["event_type"], {})
    active = [(points, product) for product, points in product_rules.items()
              if profile["products"].get(product) is True]
    if active:
        points, product = max(active)
        add("products", points, f"В предоставленной банковской записи отмечен продукт {product}; условия неизвестны.",
            [f"products.{product}"], ["event_type"], "conditional_mechanism")
    mechanism = rules["mechanism_rules"].get(event["event_type"], {}).get(industry, 0)
    if event["regions"] and not region_match:
        mechanism = 0
    filters = rules.get("mechanism_okved_filters", {}).get(event["event_type"])
    if filters and not any(code and (code == p or code.startswith(p+".")) for p in filters):
        mechanism = 0
    if mechanism:
        add("mechanism", mechanism, f"Отраслевая гипотеза чувствительности к {event['event_type']}; это не факт расходов компании.",
            ["industry"], ["event_type"], "conditional_mechanism")
    if event["event_type"] == "labor" and profile.get("employees_band"):
        add("employment", weight["labor_presence"], "Известна категория работников; потребность в найме неизвестна.",
            ["employees_band"], ["event_type"], "conditional_mechanism")
    if event["event_type"] in {"key_rate", "gdp"}:
        add("macro", weight["macro"], "Общий макроконтекст; прямую чувствительность необходимо уточнить.",
            ["company_id"], ["event_type"], "conditional_mechanism")

    entities = set(event["entity_ids"])
    resolved = event["entity_resolution"] in {"verified", "synthetic"}
    if entities and not resolved:
        warnings.append("entity_mapping_unresolved: entity_ids не использованы")
    if entities and resolved:
        if event["entity_resolution"] == "synthetic":
            warnings.append("entity_match_is_synthetic_test_only")
        if uid in entities:
            add("entity", weight["direct_entity"], "Явно сопоставленный ID компании указан в событии.",
                ["company_id"], ["entity_ids", "entity_resolution"])
        matching = [n for n in profile.get("graph", {}).get("neighbors", []) if n["company_id"] in entities]
        if uid not in entities and profile.get("graph", {}).get("status") != "loaded":
            warnings.append("graph_not_loaded: платежные связи не проверены")
        if matching:
            neighbor = max(matching, key=lambda n: n["observed_share"])
            path = f"graph.{neighbor['direction']}.{neighbor['company_id']}"
            add("counterparty", weight["counterparty_base"]+weight["counterparty_share"]*neighbor["observed_share"],
                "Совпадает наблюдаемый платежный контрагент; доля относится только к доступной выборке.",
                [path], ["entity_ids", "entity_resolution"], "conditional_mechanism")
    channels = sorted({r["group"] for r in reasons})
    if reasons:
        age_days = max(0, (now-timestamp(event["published_at"])).total_seconds()/86400)
        add("freshness", weight["freshness"]*math.exp(-math.log(2)*age_days/10),
            "Поправка на давность публикации; не оценка истинности.", [], ["published_at"])
        if event["effective_at"]:
            days = (timestamp(event["effective_at"])-now).total_seconds()/86400
            if 0 <= days <= 7:
                add("upcoming", weight["upcoming"], "Указанное изменение вступает в силу в течение недели.",
                    [], ["effective_at"])
    else:
        warnings.append("no_rule_match: релевантность неизвестна, а не доказанно нулевая")
    if industry is None:
        warnings.append("industry_unknown")
    refs = sorted({ref for r in reasons for ref in r["feature_ids"]})
    missing = list(rules["mechanism_missing_fields"].get(event["event_type"], []))
    unknown_products = [f"products.{p}" for p in product_rules if profile["products"].get(p) is None]
    return {
        "event_id": event["event_id"], "cluster_id": event["cluster_id"],
        "score": round(min(100, sum(r["contribution"] for r in reasons)), 4),
        "eligible": True, "reasons": reasons, "candidate_channels": channels,
        "features_used": {ref: profile["features"][ref] for ref in refs},
        "missing_context": sorted(set(missing+unknown_products)), "warnings": warnings,
        "evidence_status": "conditional" if reasons else "insufficient",
        "scoring_version": rules["version"], "event": event,
    }


def retrieve_candidates(profile, events, as_of, rules=None, max_candidates=None):
    """Exact scan baseline. A scalable lexical/dense retriever can replace it later."""
    rules = rules or load_rules()
    if max_candidates is not None and max_candidates < 1:
        raise ValueError("max_candidates должен быть положительным")
    current, excluded = available_versions(validate_events(events), as_of, rules["lookback_days"])
    scored = [score_event(profile, e, as_of, rules) for e in current]
    candidates = [s for s in scored if s["candidate_channels"]]
    excluded.extend({"event_id": s["event_id"], "reason": "no_candidate_signal_unknown_relevance"}
                    for s in scored if not s["candidate_channels"])
    candidates.sort(key=lambda s: (-s["score"], s["event_id"]))
    if max_candidates is not None:
        retained, removed = candidates[:max_candidates], candidates[max_candidates:]
        excluded.extend({"event_id": s["event_id"], "reason": "candidate_budget"} for s in removed)
        candidates = retained
    return candidates, excluded


def compose_feed(ranked, top_k=5, rules=None, seen_versions=None):
    rules = rules or load_rules()
    if top_k < 1:
        raise ValueError("top_k должен быть положительным")
    seen_versions = seen_versions or {}
    if any(type(value) is not int or value < 0 for value in seen_versions.values()):
        raise ValueError("Увиденные версии должны быть неотрицательными целыми")
    feed, counts, clusters = [], {}, set()
    for item in sorted(ranked, key=lambda s: (-s["score"], s["event_id"])):
        event, cluster = item["event"], item["cluster_id"]
        if not item["eligible"] or item["score"] < rules["min_feed_score"] or cluster in clusters:
            continue
        if event["version"] <= seen_versions.get(cluster, 0):
            continue
        topic = event["event_type"]
        if counts.get(topic, 0) >= rules["max_per_topic"] and item["score"] < rules["priority_override_score"]:
            continue
        feed.append(item)
        clusters.add(cluster)
        counts[topic] = counts.get(topic, 0)+1
        if len(feed) == top_k:
            break
    return feed


def rank_events(profile, events, as_of, top_k=5, rules=None, seen_versions=None):
    rules = rules or load_rules()
    prepared = validate_events(events)
    def digest(value):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    ranked, excluded = retrieve_candidates(profile, prepared, as_of, rules)
    return {"company_id": profile["company_id"], "profile_version": profile["profile_version"],
            "as_of": as_of, "scoring_version": rules["version"],
            "rules_sha256": digest(rules),
            "input_events_sha256": digest(sorted(prepared, key=lambda e: (e["event_id"], e["version"]))),
            "retrieval_policy": {"lookback_days": rules["lookback_days"], "mode": "exact_rule_scan"},
            "profile_warnings": profile.get("warnings", []),
            "missing_profile_fields": profile.get("missing_fields", []),
            "ranked": ranked, "feed": compose_feed(ranked, top_k, rules, seen_versions),
            "excluded": excluded,
            "limitations": ["эвристический score, не вероятность и не денежный ущерб",
                            "входные события уже структурированы; raw-text extraction не реализован",
                            "поиск кандидатов пока точный перебор с правилами и слабым lexical fallback, без эмбеддингов"]}
