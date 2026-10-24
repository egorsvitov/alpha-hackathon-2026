"""Read the OKVED -> activity interests catalogue (retriever/catalogues).

The catalogue maps OKVED codes to activity interests (slug, title, description).
A code matches either exactly or as a prefix when it is a longer code than the
catalogue row. Interest descriptions are the natural text used for encoding.
"""

from __future__ import annotations

import json
from pathlib import Path

_CATALOGUE = Path(__file__).parent / "activity-interests-v1" / "catalogue.json"


def _load():
    data = json.loads(_CATALOGUE.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("catalogue_must_be_array")
    return data


def load_activity_interests(okved_code: str) -> list[dict]:
    """Return interests for the OKVED code using exact or prefix matching.

    Only a non-empty result is returned; an unknown code yields an empty list.
    """
    okved = (okved_code or "").strip()
    if not okved:
        return []
    matches = []
    for row in _load():
        code = (row.get("okved_code") or "").strip()
        if not code:
            continue
        if okved == code or okved.startswith(code + "."):
            matches.extend(
                {
                    "interest_id": f"{code}/{i['slug']}",
                    "title": i.get("title"),
                    "description": i.get("description"),
                    "applicable_when": i.get("applicable_when"),
                }
                for i in row.get("interests", [])
            )
    return matches