"""Small explicit metrics; labels are human-review drafts, not independent truth."""
import math
from .events import available_versions, timestamp


def ndcg_at_k(order, grades, k=5):
    if k < 1:
        raise ValueError("k должен быть положительным")
    def dcg(values):
        return sum((2**grade-1)/math.log2(i+2) for i, grade in enumerate(values[:k]))
    ideal = dcg(sorted(grades.values(), reverse=True))
    if ideal == 0:
        return None
    seen, actual = set(), []
    for item in order:
        if item not in seen:
            actual.append(grades.get(item, 0))
            seen.add(item)
    return dcg(actual)/ideal


def precision_at_k(order, grades, k=5, threshold=2):
    if k < 1:
        raise ValueError("k должен быть положительным")
    unique = list(dict.fromkeys(order))[:k]
    # Fixed k penalizes an empty/short feed. Unknown labels must be removed upstream.
    return sum(grades.get(item, 0) >= threshold for item in unique)/k


def evaluate(result, events, labels, k=5):
    seen_labels = set()
    for label in labels:
        key = (label["company_id"], label["cluster_id"])
        grade = label["grade"]
        if key in seen_labels:
            raise ValueError(f"Повторная метка пары: {key}")
        if grade is not None and (type(grade) is not int or grade not in {0, 1, 2, 3}):
            raise ValueError(f"Значимость должна быть целым 0–3 или null: {key}")
        seen_labels.add(key)
    current, _ = available_versions(events, result["as_of"], result.get("retrieval_policy", {}).get("lookback_days", 30))
    available = {e["cluster_id"] for e in current}
    provisional = {item["cluster_id"]: item["grade"] for item in labels
                   if item["company_id"] == result["company_id"] and item["grade"] is not None
                   and item["cluster_id"] in available}
    ungraded = sorted(available-provisional.keys())
    candidate_order = [s["cluster_id"] for s in result["ranked"]]
    feed_order = [s["cluster_id"] for s in result["feed"]]
    chrono_order = [e["cluster_id"] for e in sorted(current,
                     key=lambda e: (timestamp(e["published_at"]), e["event_id"]), reverse=True)]
    relevant = {key for key, grade in provisional.items() if grade >= 2}
    metrics = {}
    for name, order in [("ranked", candidate_order), ("feed", feed_order), ("freshness_baseline", chrono_order)]:
        if ungraded:
            # Do not implicitly classify unjudged events as negatives.
            metrics[name] = {f"ndcg_at_{k}": None, f"precision_at_{k}": None,
                             "reason": "incomplete_judgments_in_available_pool"}
        else:
            metrics[name] = {f"ndcg_at_{k}": ndcg_at_k(order, provisional, k),
                             f"precision_at_{k}": precision_at_k(order, provisional, k)}
    return {"company_id": result["company_id"], "as_of": result["as_of"],
            "scoring_version": result["scoring_version"],
            "label_status": "provisional_not_expert_reviewed", "independent_benchmark": False,
            "graded_available_clusters": len(provisional), "ungraded_clusters": ungraded,
            "candidate_recall_relevance_ge_2": (len(relevant & set(candidate_order))/len(relevant) if relevant else None),
            "important_grade_3_missed": sorted(key for key, grade in provisional.items()
                                              if grade == 3 and key not in candidate_order),
            "metrics": metrics, "precision_definition": "relevance>=2; denominator fixed k; k="+str(k),
            "ndcg_definition": "gain=2**grade-1; no positive judgments => null"}
