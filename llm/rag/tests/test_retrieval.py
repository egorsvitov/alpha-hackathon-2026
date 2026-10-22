"""Fully synthetic, independent of private PDF/OCR files."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator, FormatChecker
from llm.rag.corpus import Corpus, text_hash
from llm.rag.retriever import load_config, retrieve_evidence


ROOT = Path(__file__).resolve().parents[3]


def chunk(text="Учебная ключевая ставка и кредитование.", **changes):
    result = {"chunk_id": "synthetic:" + text_hash(text), "document_id": "synthetic-document",
              "document_sha256": "a" * 64, "source_path": "synthetic.pdf", "page": 1,
              "char_start": 0, "char_end": len(text), "text": text, "text_sha256": text_hash(text),
              "published_at": "2026-10-19", "date_status": "synthetic", "title": "Вымышленный обзор",
              "verification_status": "synthetic", "topics": ["policy_rate"], "historical_only": False,
              "retrieval_eligible": True, "source_kind": "synthetic", "extraction": "synthetic"}
    result.update(changes)
    return result


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "chunks.jsonl"
        self.config = load_config()
        self.bundle = {"event": {"event_id": "synthetic:event", "event_version": 1, "title": "Ставка"},
                       "ranking_features": {"topics": ["rates"]}, "claims": []}
        self.profile = {"company_id": "SYNTHETIC", "profile_version": "synthetic-v1"}
        self.as_of = datetime.fromisoformat("2026-10-20T18:00:00+03:00")
        schema = json.loads((ROOT / "docs/contracts/evidence-bundle.schema.json").read_text())
        Draft202012Validator.check_schema(schema)
        self.validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def corpus(self, chunks):
        self.path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks))
        return Corpus(self.path)

    def retrieve(self, corpus, **kwargs):
        result = retrieve_evidence(self.bundle, self.profile, {}, as_of=kwargs.pop("as_of", self.as_of),
                                   corpus=corpus, config=kwargs.pop("config", self.config), **kwargs)
        self.validator.validate(result)
        return result

    def test_G01_mapping_synonyms_and_unknown_factor(self):
        self.bundle["ranking_features"]["factors"] = ["unmapped_factor"]
        result = self.retrieve(self.corpus([chunk("Учебные ставки по кредитам определяют прогноз.")]))
        self.assertEqual(len(result["hits"]), 1)
        self.assertIn("policy_rate", result["query_plan"][0]["topic_filters"])

    def test_G02_temporal_and_exclusion_before_topk(self):
        chunks = [chunk(chunk_id="future", published_at="2026-10-21"), chunk(chunk_id="unknown", published_at=None),
                  chunk(chunk_id="historical", historical_only=True), chunk(chunk_id="excluded", retrieval_eligible=False),
                  chunk(chunk_id="available")]
        result = self.retrieve(self.corpus(chunks))
        self.assertEqual([h["chunk_id"] for h in result["hits"]], ["available"])

    def test_G03_date_only_boundary_next_moscow_day(self):
        corpus = self.corpus([chunk(published_at="2026-10-20")])
        for time in ("2026-10-20T00:01:00+03:00", "2026-10-20T23:59:59+03:00"):
            self.assertFalse(self.retrieve(corpus, as_of=datetime.fromisoformat(time))["hits"])
        self.assertTrue(self.retrieve(corpus, as_of=datetime.fromisoformat("2026-10-21T00:00:00+03:00"))["hits"])
        daily = dict(self.config, temporal_mode="daily")
        self.assertIn("intraday_availability_unproven", self.retrieve(corpus, config=daily)["hits"][0]["limitations"])

    def test_G04_versions_text_metadata_exclusion(self):
        original = self.corpus([chunk()]).version
        for changed in (chunk("Другая ставка."), chunk(published_at="2026-10-18"), chunk(retrieval_eligible=False)):
            self.assertNotEqual(self.corpus([changed]).version, original)

    def test_G05_quote_hash_page_unicode_and_tampering(self):
        c = chunk("😀 Учебная ставка.")
        corpus = self.corpus([c])
        hit = self.retrieve(corpus)["hits"][0]
        self.assertTrue(corpus.verify_hit(hit))
        for key, value in (("quote", "ошибка"), ("page", 2), ("char_end", 500), ("text_sha256", "b"*64)):
            with self.assertRaises(ValueError):
                corpus.verify_hit(dict(hit, **{key: value}))
        c["text"] += "broken"
        with self.assertRaises(ValueError):
            self.corpus([c])

    def test_G05_source_page_and_document_checks(self):
        c = chunk()
        (Path(self.temp.name) / "source.pdf").write_bytes(b"synthetic pdf bytes")
        c["source_path"] = str(Path(self.temp.name) / "source.pdf")
        with self.assertRaisesRegex(ValueError, "document_sha256"):
            self.corpus([c])
        c = chunk()
        directory = Path(self.temp.name) / "ocr" / c["document_id"]
        directory.mkdir(parents=True)
        (directory / "page-001.txt").write_text("wrong page")
        with self.assertRaisesRegex(ValueError, "OCR page"):
            self.corpus([c])

    def test_G06_overlap_dedup_and_budgets(self):
        first = chunk("Ставка кредитование ставка прогноз.")
        second = chunk(first["text"][7:], chunk_id="overlap", char_start=7, char_end=len(first["text"]))
        result = self.retrieve(self.corpus([first, second]))
        self.assertEqual(len(result["hits"]), 1)
        tiny = dict(self.config, max_context_chars=5)
        self.assertFalse(self.retrieve(self.corpus([first]), config=tiny)["hits"])

    def test_G07_empty_missing_no_match_pending_OCR(self):
        self.assertEqual(self.retrieve(Corpus(self.path))["status"], "unavailable")
        empty = self.retrieve(self.corpus([]))
        self.assertEqual(empty["status"], "no_bank_evidence")
        self.assertIsNotNone(empty["corpus_version"])
        result = self.retrieve(self.corpus([chunk(verification_status="pending_review", source_kind="bank_analytics")]))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["hits"][0]["verification_status"], "pending_review")

    def test_G08_retry_bounded_and_dates_preserved(self):
        corpus = self.corpus([chunk(published_at=None), chunk(chunk_id="future", published_at="2027-01-01")])
        plans = []
        for attempt in range(3):
            result = self.retrieve(corpus, attempt=attempt, retry_reason="missing_subject" if attempt else None)
            self.assertFalse(result["hits"])
            self.assertEqual(len(result["query_plan"]), 1)
            plans.append(result["query_plan"][0]["queries"])
        self.assertEqual(len({str(p) for p in plans}), 3)
        for attempt in (-1, 3, True):
            with self.assertRaises(ValueError):
                self.retrieve(corpus, attempt=attempt)
        with self.assertRaises(ValueError):
            self.retrieve(corpus, attempt=1)

    def test_weak_common_terms_cannot_admit(self):
        result = self.retrieve(self.corpus([chunk("Прогноз России: компания и перспективы развития.")]))
        self.assertEqual(result["status"], "no_bank_evidence")
        self.assertIn("insufficient_relevance", result["limitations"])

    def test_lexical_fallback_needs_two_content_terms(self):
        self.bundle["event"]["title"] = "Контейнеры логистика"
        self.bundle["ranking_features"] = {"factors": ["unmapped"]}
        result = self.retrieve(self.corpus([chunk("Контейнеры.", chunk_id="one"),
                                           chunk("Контейнеры логистика.", chunk_id="two")]))
        self.assertEqual([h["chunk_id"] for h in result["hits"]], ["two"])

    def test_unknown_and_historical_only_diagnostic_in_research(self):
        config = dict(self.config, temporal_mode="research_current")
        result = self.retrieve(self.corpus([chunk(published_at=None), chunk(chunk_id="old", historical_only=True)]), config=config)
        self.assertFalse(result["hits"])
        self.assertEqual(len(result["diagnostics"]["rejected"]), 2)

    def test_duplicate_ids_and_invalid_date(self):
        with self.assertRaises(ValueError):
            self.corpus([chunk(), chunk()])
        for date in ("2026-02-30", "2026-10-20T12:00:00"):
            with self.assertRaises(ValueError):
                self.corpus([chunk(published_at=date)])

    def test_visual_status_requires_exact_record(self):
        with self.assertRaises(ValueError):
            self.corpus([chunk(verification_status="visually_checked_excerpt")])

    def test_synthetic_fixture_and_default_config(self):
        corpus = Corpus(Path(__file__).parent / "fixtures/corpus.jsonl")
        result = self.retrieve(corpus, config={})
        self.assertTrue(result["hits"][0]["is_synthetic"])
        fixture = json.loads((Path(__file__).parent / "fixtures/evidence.json").read_text())
        self.validator.validate(fixture)
        corpus.verify_hit(fixture["hits"][0])

    def test_catalog_changes_corpus_version_and_excludes(self):
        corpus = self.corpus([chunk()])
        version = corpus.version
        c = chunk()
        catalog = {"synthetic.pdf": {k: c[k] for k in ("published_at", "date_status", "title", "topics", "historical_only")}}
        catalog["synthetic.pdf"]["exclude_pages"] = [1]
        (Path(self.temp.name) / "document_catalog.json").write_text(json.dumps(catalog))
        reloaded = Corpus(self.path)
        self.assertNotEqual(version, reloaded.version)
        self.assertFalse(self.retrieve(reloaded)["hits"])

    def test_pinned_index_no_repeat_source_reads(self):
        from unittest.mock import patch
        corpus = self.corpus([chunk()])
        with patch.object(Path, "read_text", side_effect=AssertionError("repeat read")), patch.object(Path, "read_bytes", side_effect=AssertionError("repeat read")):
            first = self.retrieve(corpus)
            second = self.retrieve(corpus)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
