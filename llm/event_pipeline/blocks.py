"""Deterministic original-text slices, never reconstructed quotations."""

import re
from .models import SourceBlock


def source_blocks(document, limit=2000):
    if type(limit) is not int or limit < 1:
        raise ValueError("invalid_block_limit")
    result = []
    technical = any(
        p.get("title_origin") == "generated_identifier"
        for p in document.get("provenance", [])
    )
    for field in ("title", "text"):
        if field == "title" and technical:
            continue
        text = document["article"][field]
        for paragraph in re.finditer(r"\S.*?(?=\n\s*\n|\Z)", text, re.S):
            start, end = paragraph.span()
            while start < end:
                stop = min(start + limit, end)
                if stop < end:
                    choices = list(re.finditer(r"[.!?]\s+|\s+", text[start:stop]))
                    if choices:
                        sentences = [m for m in choices if m.group()[0] in ".!?"]
                        stop = start + (sentences or choices)[-1].end()
                result.append(
                    SourceBlock(
                        block_id=f"{document['document_id']}:{field}:{start}:{stop}",
                        document_id=document["document_id"],
                        field=field,
                        start=start,
                        end=stop,
                        text=text[start:stop],
                    )
                )
                start = stop
    return result


def packets(blocks, limit=8000):
    if limit < 1:
        raise ValueError("invalid_packet_limit")
    title = [b for b in blocks if b.field == "title"]
    body = [b for b in blocks if b.field == "text"]
    if not body:
        body, title = title, []
    i = 0
    while i < len(body):
        j, size = i, 0
        while j < len(body) and (j == i or size + len(body[j].text) <= limit):
            size += len(body[j].text)
            j += 1
        yield {
            "primary": [b.block_id for b in body[i:j]],
            "blocks": [
                b.model_dump()
                for b in title + body[max(0, i - 1) : min(len(body), j + 1)]
            ],
        }
        i = j


def packet_for(blocks, primary_ids):
    """Include adjacent blocks and conservatively preserve tabular headings."""
    primary = set(primary_ids)
    positions = [i for i, b in enumerate(blocks) if b.block_id in primary]
    selected = {
        i for p in positions for i in range(max(0, p - 1), min(len(blocks), p + 2))
    }
    selected.update(i for i, b in enumerate(blocks) if b.field == "title")
    for p in positions:
        if "\t" in blocks[p].text or "|" in blocks[p].text:
            first = p
            while first and (
                "\t" in blocks[first - 1].text or "|" in blocks[first - 1].text
            ):
                first -= 1
            selected.add(first)
            if first:
                selected.add(first - 1)
    return {
        "primary": primary_ids,
        "blocks": [blocks[i].model_dump() for i in sorted(selected)],
    }


def fit_packets(blocks, packet_limit, max_bytes, make_request):
    """Split before dispatch; never trim a block or its necessary context."""
    import json

    def fit(ids):
        packet = packet_for(blocks, ids)
        size = len(
            json.dumps(
                make_request(packet),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        if size <= max_bytes:
            yield packet
        elif len(ids) > 1:
            middle = len(ids) // 2
            yield from fit(ids[:middle])
            yield from fit(ids[middle:])
        else:
            yield {**packet, "deferred_reason": "input_too_large"}

    for packet in packets(blocks, packet_limit):
        yield from fit(packet["primary"])
