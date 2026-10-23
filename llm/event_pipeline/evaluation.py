"""Gold-mention retrieval measurement, separate from extraction quality."""

from .models import EventMention
from .blocks import source_blocks
from .retrieval import BM25Retriever


def evaluate_gold_retrieval(documents, gold, top_k=20, max_clusters=8):
    blocks = {b.block_id: b for d in documents for b in source_blocks(d)}
    order = {d["document_id"]: i for i, d in enumerate(documents)}
    events = {}
    docs = {d["document_id"]: d for d in documents}
    for item in gold["mentions"]:
        if item["document_id"] not in docs or not item["block_ids"]:
            raise ValueError("invalid_gold_source")
        if any(
            b not in blocks or blocks[b].document_id != item["document_id"]
            for b in item["block_ids"]
        ):
            raise ValueError("invalid_gold_blocks")
        anchor = item.get("anchor")
        if anchor:
            text = docs[item["document_id"]]["article"][anchor["field"]]
            if text[anchor["start"] : anchor["end"]] != anchor["text"]:
                raise ValueError("invalid_gold_anchor")
        if item.get("event_id") and item.get("split"):
            splits = events.setdefault(item["event_id"], set())
            splits.add(item["split"])
            if len(splits) > 1:
                raise ValueError("event_split_leakage")
    items = sorted(
        gold["mentions"],
        key=lambda g: (
            order[g["document_id"]],
            min(blocks[b].start for b in g["block_ids"]),
            g["id"],
        ),
    )
    previous = []
    memberships = {}
    cases = []
    retriever = BM25Retriever(top_k, max_clusters)
    for g in items:
        mention = EventMention(
            mention_id=g["id"],
            document_id=g["document_id"],
            block_ids=g["block_ids"],
            gloss=g.get("gloss"),
        )
        result = retriever.search(mention, previous, blocks, memberships)
        event = g.get("event_id")
        if event and event in memberships.values():
            cases.append(
                {
                    "mention_id": g["id"],
                    "event_id": event,
                    "found": event in result["cluster_ids"],
                    "candidates": len(result["cluster_ids"]),
                }
            )
        memberships[g["id"]] = event or "unknown:" + g["id"]
        previous.append(mention)
    return {
        "evaluation": "retrieval_on_manual_mentions",
        "eligible_mentions": len(cases),
        "candidate_recall": sum(c["found"] for c in cases) / len(cases)
        if cases
        else None,
        "mean_candidates": sum(c["candidates"] for c in cases) / len(cases)
        if cases
        else None,
        "cases": cases,
        "limitations": [
            "Uses supplied gold mentions and glosses, not LLM extraction. Recall is for annotated repeated events only."
        ],
    }
