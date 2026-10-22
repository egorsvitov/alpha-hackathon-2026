"""Helpers for human-authored fixtures: IDs/spans only, never infer facts."""

from datetime import datetime

from llm.events.contracts.models import ArticleInput
from llm.events.contracts.validation import article_sha256


def period(text, start=None, end=None):
    return {"start": start, "end": end if end is not None else start, "text": text}


def measurement(metric, value, unit, *, kind="level", role="reported", qualifier="exact", comparison=None):
    return {"measurement_id": "assigned_by_fixture_builder", "metric": metric, "kind": kind,
            "value": value, "unit": unit, "qualifier": qualifier,
            "comparison_period": comparison, "value_role": role}


class AnnotationBuilder:
    """Author must supply every semantic label and every exact quotation."""

    def __init__(self, articles, dataset_kind):
        self.records = {}
        for a in articles:
            article = ArticleInput.model_validate(a)
            self.records[article.article_id] = {"article": article.model_dump(mode="json"),
                "article_version": 1, "source_id": article.article_id + ":v1"}
        self.kind = dataset_kind
        self.bundles = {}
        self.annotations = {aid: {"article_id": aid, "article_version": 1,
            "article_sha256": article_sha256(ArticleInput.model_validate(r["article"])),
            "claims": [], "excluded": [], "limitations": []} for aid, r in self.records.items()}
        self.links = []

    def claim(self, aid, local_id, event_id, event_type, text, quotes, *, modality="asserted",
              polarity="affirmed", stage=None, actor=None, attribution=None, action=None,
              object=None, geographies=(), occurred=None, effective=None, observation=None,
              horizon=None, measurements=(), scenario=None, topics=("other",),
              industries=(), factors=(), note="Принадлежность задана вручную по субъекту, предмету и времени.",
              uncertain=False):
        record = self.records[aid]
        article = record["article"]
        cid = aid + ":" + local_id
        source = {"source_id": record["source_id"], "article_id": aid,
            "article_version": record["article_version"], "title": article["title"],
            "publisher": article["source"], "url": article["url"],
            "published_at": article["published_at"], "content_kind": article["content_kind"],
            "is_full_original_text": article["is_full_original_text"], "is_synthetic": article["is_synthetic"]}
        evidence = []
        for i, quote in enumerate(quotes):
            start = article["text"].find(quote)
            if start < 0 or article["text"].find(quote, start + 1) >= 0:
                raise ValueError(f"Missing or ambiguous quote {aid}: {quote}")
            evidence.append({"evidence_id": f"{cid}:e{i+1}", "source_id": record["source_id"],
                "field": "text", "start": start, "end": start + len(quote), "quote": quote})
        ms = [dict(m, measurement_id=f"{cid}:m{i+1}") for i, m in enumerate(measurements)]
        claim = {"claim_id": cid, "text": text, "modality": modality, "polarity": polarity,
            "stage": stage, "attribution": attribution,
            "participants": [actor] if actor else [], "action": action, "object": object,
            "geographies": list(geographies), "time": {"occurred_at": occurred, "effective_at": effective,
                "observation_period": observation, "forecast_horizon": horizon},
            "measurements": ms, "evidence_ids": [e["evidence_id"] for e in evidence],
            "field_evidence": {}, "scenario": scenario}
        # Coarse but explicit support: all manually selected excerpts back each populated group.
        for key in ("text", "modality", "polarity", "stage", "attribution", "participants", "action", "object", "geographies", "time", "measurements", "scenario"):
            if claim[key] is not None and claim[key] not in ([], {}):
                if key == "time" and not any(claim[key].values()):
                    continue
                claim["field_evidence"]["/" + key] = claim["evidence_ids"].copy()
        if event_id not in self.bundles:
            self.bundles[event_id] = {"schema_version": "0.2",
                "event": {"event_id": event_id, "event_version": 1, "title": text, "event_type": event_type,
                    "first_published_at": article["published_at"], "updated_at": "2026-10-21T14:00:00+03:00",
                    "availability": "active", "quality": "complete"},
                "ranking_features": {"topics": [], "industries": [], "geographies": [], "objects": [], "metrics": [], "factors": []},
                "claims": [], "sources": [], "evidence": [], "conflicts": [], "relations": [],
                "limitations": ["Ручная разметка одного аннотатора; смысловая корректность независимо не проверена."]}
        bundle = self.bundles[event_id]
        if bundle["event"]["event_type"] != event_type:
            raise ValueError("Event type mismatch")
        bundle["claims"].append(claim)
        if source["source_id"] not in {s["source_id"] for s in bundle["sources"]}:
            bundle["sources"].append(source)
        bundle["evidence"].extend(evidence)
        dates = [s["published_at"] for s in bundle["sources"] if s["published_at"]]
        bundle["event"]["first_published_at"] = min(dates, key=datetime.fromisoformat) if dates else None
        features = {"topics": topics, "industries": industries, "geographies": geographies,
                    "objects": [object] if object else [], "metrics": [m["metric"] for m in ms], "factors": factors}
        for key, labels in features.items():
            bundle["ranking_features"][key] = sorted(set(bundle["ranking_features"][key]) | set(labels))
        self.annotations[aid]["claims"].append({"claim_id": cid, "event_type": event_type,
            "expected_event_id": None if uncertain else event_id, "identity_note": note})
        return cid

    def link(self, left, right, relation, reason):
        self.links.append({"from_claim_id": left, "to_claim_id": right, "relation": relation, "reason": reason})

    def exclude(self, aid, quote, reason):
        self.annotations[aid]["excluded"].append({"quote": quote, "reason": reason})

    def result(self):
        return {"schema_version": "0.2", "annotation_version": "b1.1",
            "annotation_status": "single_annotator_draft", "dataset_kind": self.kind,
            "articles": list(self.records.values()), "annotations": list(self.annotations.values()),
            "bundles": list(self.bundles.values()), "expected_links": self.links}
