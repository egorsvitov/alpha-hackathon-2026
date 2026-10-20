"""Small lexical BM25 baseline over draft OCR chunks; not a production RAG."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re


def tokens(text):
    # Simple prefix normalization for a Russian baseline; replace after evaluation.
    return [w[:6] if len(w) > 6 else w for w in re.findall(r"[а-яёa-z0-9]+", text.lower())]


def search(query, top_k=5, topic=None, as_of=None, include_historical=False):
    chunks = [json.loads(s) for s in Path("data/processed/rag_chunks.jsonl").read_text().splitlines()]
    chunks = [c for c in chunks if c["retrieval_eligible"]
              and (include_historical or not c["historical_only"])
              and (topic is None or topic in c["topics"])
              # Undated materials cannot safely enter an as-of replay.
              and (as_of is None or (c["published_at"] and c["published_at"] <= as_of))]
    bags = [Counter(tokens(c["text"])) for c in chunks]
    lengths = [sum(b.values()) for b in bags]
    avg = sum(lengths)/max(1, len(lengths))
    df = Counter(word for bag in bags for word in bag)
    scored = []
    for c, bag, length in zip(chunks, bags, lengths):
        score = 0
        for word in set(tokens(query)):
            freq = bag[word]
            if freq:
                idf = math.log(1+(len(chunks)-df[word]+.5)/(df[word]+.5))
                score += idf*freq*2.2/(freq+1.2*(.25+.75*length/max(1, avg)))
        if score:
            scored.append({"score": round(score, 4), **c})
    return sorted(scored, key=lambda x: x["score"], reverse=True)[:top_k]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--topic")
    parser.add_argument("--as-of", help="YYYY-MM-DD; excludes unknown publication dates")
    parser.add_argument("--include-historical", action="store_true")
    args = parser.parse_args()
    print(json.dumps(search(args.query, args.top_k, args.topic, args.as_of,
                            args.include_historical), ensure_ascii=False, indent=2))
