"""Deterministic complete-link grouping; no model or SDK imports."""

import hashlib
import heapq
import json
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

VERSION = "semantic-groups.v1"


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalize(vector):
    if not vector or any(
        type(x) not in (int, float) or not math.isfinite(x) for x in vector
    ):
        raise ValueError("invalid_embedding")
    norm = math.sqrt(sum(x * x for x in vector))
    if not math.isfinite(norm) or norm == 0:
        raise ValueError("invalid_embedding_norm")
    return [x / norm for x in vector]


def publication(document):
    """Preserve date precision. A calendar date is an interval, not midnight."""
    raw = document["article"].get("published_at")
    if raw:
        value = datetime.fromisoformat(raw)
        if value.tzinfo is None:
            raise ValueError("publication_timezone_missing")
        return {"precision": "timestamp", "value": raw}
    dates = {
        p.get("publication", {}).get("date") for p in document.get("provenance", [])
    } - {None}
    if len(dates) == 1:
        raw = next(iter(dates))
        date.fromisoformat(raw)
        return {"precision": "day", "value": raw}
    return {"precision": "unknown", "value": None}


def interval(publication):
    precision, value = publication["precision"], publication["value"]
    if precision == "unknown":
        return None
    if precision == "timestamp":
        instant = datetime.fromisoformat(value).astimezone(timezone.utc).timestamp()
        return instant, instant
    start = datetime.combine(
        date.fromisoformat(value), datetime.min.time(), ZoneInfo("Europe/Moscow")
    )
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def time_gap(left, right):
    a, b = interval(left), interval(right)
    if a is None or b is None:
        return None
    return max(0, a[0] - b[1], b[0] - a[1]) / 86400


def validate_config(threshold, alpha, half_life_days):
    if not all(
        type(x) in (int, float) and math.isfinite(x)
        for x in (threshold, alpha, half_life_days)
    ):
        raise ValueError("invalid_grouping_parameters")
    if not 0 < threshold <= 1 or not 0 <= alpha <= 1 or half_life_days <= 0:
        raise ValueError("invalid_grouping_parameters")


def pair_score(left, right, a, b, alpha=0.4, half_life_days=7):
    cosine = max(-1.0, min(1.0, sum(x * y for x, y in zip(left, right, strict=True))))
    gap = time_gap(a, b)
    multiplier = (
        1.0 if gap is None else (1 - alpha) + alpha * 2 ** (-gap / half_life_days)
    )
    return {
        "cosine": cosine,
        "gap_days": gap,
        "time_multiplier": multiplier,
        "score": max(0.0, cosine) * multiplier,
    }


def complete_link(scores, threshold):
    """Small dependency-free reference; ties resolved by sorted leaf indices."""
    n = len(scores)
    members = {i: [i] for i in range(n)}
    distances, heap = {}, []
    for i in range(n):
        for j in range(i + 1, n):
            distances[i, j] = scores[i][j]
            heapq.heappush(heap, (-scores[i][j], i, j))
    next_id = n
    while heap:
        negative, a, b = heapq.heappop(heap)
        if a not in members or b not in members:
            continue
        if -negative < threshold:
            break
        merged = sorted(members[a] + members[b])
        del members[a], members[b]
        for other in sorted(members):
            value = min(
                distances[tuple(sorted((a, other)))],
                distances[tuple(sorted((b, other)))],
            )
            distances[other, next_id] = value
            heapq.heappush(heap, (-value, other, next_id))
        members[next_id] = merged
        next_id += 1
    return sorted(members.values(), key=lambda x: x[0])


def group_vectors(
    vectors,
    dates,
    *,
    threshold=0.75,
    alpha=0.0,
    half_life_days=7,
    backend="reference",
    max_units=5000,
    max_matrix_mb=512,
    neighbors=10,
):
    validate_config(threshold, alpha, half_life_days)
    n = len(vectors)
    if n != len(dates) or n > max_units or n * n * 40 > max_matrix_mb * 1024**2:
        raise ValueError("grouping_size_limit")
    if type(neighbors) is not int or neighbors < 0:
        raise ValueError("invalid_neighbors")
    if backend not in ("reference", "scipy"):
        raise ValueError("unknown_backend")
    if backend == "reference" and n > 512:
        raise ValueError("use_scipy_for_more_than_512_units")
    vectors = [normalize(v) for v in vectors]
    if len({len(v) for v in vectors}) > 1:
        raise ValueError("embedding_dimension_mismatch")
    if backend == "scipy":
        import numpy as np
        from scipy.cluster.hierarchy import linkage, fcluster
        from scipy.spatial.distance import squareform

        matrix = np.asarray(vectors, dtype=np.float64)
        scores = np.clip(matrix @ matrix.T, 0, 1) if n else np.zeros((0, 0))
        if alpha:
            for i in range(n):
                for j in range(i + 1, n):
                    gap = time_gap(dates[i], dates[j])
                    factor = (
                        1
                        if gap is None
                        else (1 - alpha) + alpha * 2 ** (-gap / half_life_days)
                    )
                    scores[i, j] *= factor
                    scores[j, i] = scores[i, j]
        if n > 1:
            distance = 1 - scores
            np.fill_diagonal(distance, 0)
            labels = fcluster(
                linkage(squareform(distance, checks=False), method="complete"),
                t=1 - threshold,
                criterion="distance",
            )
            grouped = {}
            for i, label in enumerate(labels):
                grouped.setdefault(int(label), []).append(i)
            groups = sorted(grouped.values(), key=lambda g: g[0])
        else:
            groups = [[0]] if n else []
    else:
        scores = [
            [
                pair_score(a, b, dates[i], dates[j], alpha, half_life_days)["score"]
                for j, b in enumerate(vectors)
            ]
            for i, a in enumerate(vectors)
        ]
        groups = complete_link(scores, threshold)
    edges = []
    for i in range(n):
        for j in sorted(
            (j for j in range(n) if j != i), key=lambda j: (-float(scores[i][j]), j)
        )[:neighbors]:
            edges.append(
                {
                    "left": i,
                    "right": j,
                    **pair_score(
                        vectors[i],
                        vectors[j],
                        dates[i],
                        dates[j],
                        alpha,
                        half_life_days,
                    ),
                }
            )
    return {
        "groups": groups,
        "neighbors": edges,
        "minimum_scores": [
            min((float(scores[i][j]) for i in g for j in g if i < j), default=None)
            for g in groups
        ],
    }
