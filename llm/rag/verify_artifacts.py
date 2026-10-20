"""Check corpus provenance, date boundaries and five manual retrieval probes."""
import hashlib
import json
from pathlib import Path
from search_rag import search


def main():
    chunks = [json.loads(s) for s in Path("data/processed/rag_chunks.jsonl").read_text().splitlines()]
    assert len({c["chunk_id"] for c in chunks}) == len(chunks)
    for c in chunks:
        page = (Path("data/processed/ocr") / c["document_id"] / f"page-{c['page']:03d}.txt").read_text()
        assert page[c["char_start"]:c["char_end"]] == c["text"]
        assert hashlib.sha256(c["text"].encode()).hexdigest() == c["text_sha256"]
    for d in json.loads(Path("data/processed/pdf_manifest.json").read_text()):
        assert hashlib.sha256(Path(d["path"]).read_bytes()).hexdigest() == d["sha256"]
    # Referential checks complement (and do not replace) JSON Schema validation.
    example = json.loads(Path("llm/agent/contracts/insight.example.json").read_text())
    sources = {s["source_id"]: s for s in example["sources"]}
    feature_ids = {f["feature_id"] for f in example["features_used"]}
    chunks_by_id = {c["chunk_id"]: c for c in chunks}
    for claim in example["claims"]:
        assert set(claim["source_ids"]) <= sources.keys()
        assert set(claim["feature_ids"]) <= feature_ids
    assert set(example["changes_since_previous"]["claim_ids"]) <= {
        c["claim_id"] for c in example["claims"]}
    for source in sources.values():
        assert Path(source["locator"].split("#")[0]).exists()
        if source["type"] == "bank_analytics":
            chunk = chunks_by_id[source["chunk_id"]]
            assert source["quote"] in chunk["text"]
            assert source["document_sha256"] == chunk["document_sha256"]
            assert source["page"] == chunk["page"]
    assert all(not c["historical_only"] and c["retrieval_eligible"]
               for c in search("сталь прогноз ставка", 1000))
    assert all(c["published_at"] and c["published_at"] <= "2026-08-01"
               for c in search("ставка прогноз инфляция золото", 1000, as_of="2026-08-01"))
    probes = [
        ("сохранение ставки прогноз конец года", "policy_rate", "ALFA-Macro_Insights-Policy_Rate_11Sep.pdf", 1),
        ("инфляция бензин август", "inflation", "Inflation_tracker_02.pdf", 1),
        ("золото покупки центробанков", "gold", "Gold_digest_August26.pdf", 7),
        ("рынок труда вакансии зарплаты", "labor", "ALFA-7M26.pdf", 1),
        ("ВВП инвестиции прогноз", "gdp", "ALFA-macro_stat1H26.pdf", 1),
    ]
    results = []
    for query, topic, filename, page in probes:
        hits = search(query, 3, topic=topic)
        found = any(Path(c["source_path"]).name == filename and c["page"] == page for c in hits)
        results.append({"query": query, "topic_filter": topic, "target_page_in_top3": found,
                        "hits": [{"file": c["source_path"], "page": c["page"], "score": c["score"]} for c in hits]})
        assert found, query
    report = {
        "provenance_checks": "passed",
        "temporal_and_history_filters": "passed",
        "manual_contract_example_references": "passed",
        "json_schema_draft_2020_12_validation": "not_run",
        "chunks_total": len(chunks),
        "retrieval_eligible": sum(c["retrieval_eligible"] for c in chunks),
        "default_search_chunks": sum(c["retrieval_eligible"] and not c["historical_only"] for c in chunks),
        "retrieval_probes": results,
        "limitation": "Five manually selected topic-filtered smoke probes, not an independent retrieval-quality benchmark. OCR numbers and all chart interpretations remain unverified."
    }
    Path("data/processed/validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
