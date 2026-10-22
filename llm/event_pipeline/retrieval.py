"""Two-view BM25; scores retrieve candidates, never establish identity."""

from collections import Counter, defaultdict
from math import log
import re
from typing import Protocol

STOP = frozenset(
    "и в во на с со к от до по за из у о а но или для при это как что".split()
)


def terms(text):
    return [t for t in re.findall(r"[^\W_]+", text.casefold()) if t not in STOP]


class CandidateRetriever(Protocol):
    def search(self, mention, previous, blocks, memberships): ...


class BM25Retriever:
    def __init__(self, top_k=20, max_clusters=8):
        self.top_k, self.max_clusters = top_k, max_clusters

    def search(self, mention, previous, blocks, memberships):
        def view(m, semantic):
            if semantic:
                return terms(
                    (m.gloss or "")
                    + " "
                    + " ".join(
                        v if isinstance(v, str) else " ".join(v)
                        for v in m.hints.values()
                    )
                )
            return terms(
                " ".join(
                    blocks[b].text
                    for b in dict.fromkeys(m.block_ids + m.context_block_ids)
                )
            )

        fused, ranks = defaultdict(float), defaultdict(dict)
        for semantic in (False, True):
            counts = {m.mention_id: Counter(view(m, semantic)) for m in previous}
            avg = (
                sum(sum(c.values()) for c in counts.values()) / max(1, len(counts)) or 1
            )
            query = set(view(mention, semantic))
            scores = defaultdict(float)
            for token in query:
                df = sum(token in c for c in counts.values())
                idf = log(1 + (len(counts) - df + 0.5) / (df + 0.5))
                for mid, c in counts.items():
                    tf = c[token]
                    if tf:
                        scores[mid] += (
                            idf
                            * tf
                            * 2.2
                            / (tf + 1.2 * (0.25 + 0.75 * sum(c.values()) / avg))
                        )
            for rank, (mid, score) in enumerate(
                sorted(scores.items(), key=lambda p: (-p[1], p[0]))[: self.top_k], 1
            ):
                fused[mid] += 1 / (60 + rank)
                ranks[mid]["hints" if semantic else "source"] = {
                    "rank": rank,
                    "score": score,
                }
        clusters = {}
        for mid, score in fused.items():
            cid = memberships[mid]
            clusters[cid] = max(clusters.get(cid, 0), score)
        ordered = sorted(clusters, key=lambda c: (-clusters[c], c))
        return {
            "cluster_ids": ordered[: self.max_clusters],
            "omitted_cluster_ids": ordered[self.max_clusters :],
            "mentions": [
                {"mention_id": mid, "score": fused[mid], "views": ranks[mid]}
                for mid in sorted(fused, key=lambda m: (-fused[m], m))
            ],
        }
