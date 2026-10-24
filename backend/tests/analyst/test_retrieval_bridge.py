"""Стык «адаптер нового ретривера → мост в контракт фронта»: признак синтетики, координаты цитат, формат входа.

Без сети: синтетические фикстуры ``retrieval_fixtures`` и FakeLLM.
"""
from __future__ import annotations

import unittest

from app.analyst.batch_adapter import BatchValidationError
from app.analyst.bridge import EvidenceCollector
from app.analyst.retrieval_adapter import adapt_retrieval_batch
from tests.analyst.helpers import make_service
from tests.analyst.retrieval_fixtures import batch, contexts


def adapted_candidate():
    p, i = contexts()
    inp = adapt_retrieval_batch(batch(), p, i, "cfo").analyst_input
    return inp, inp.candidates[0]


class SyntheticFlagTests(unittest.TestCase):
    def test_unknown_source_flag_is_real_news_not_demo(self):
        inp, cand = adapted_candidate()
        bundle = cand.analysis_projection.event_bundles[0]
        self.assertIsNone(bundle.sources[0].is_synthetic, "адаптер не знает признака синтетики")
        col = EvidenceCollector(cand, inp.company_context)
        ev = bundle.evidence[0]
        self.assertEqual(col.add_evidence(ev.evidence_id), ev.evidence_id)
        item = col.items[0]
        self.assertEqual((item.type, item.is_synthetic), ("news", False))

    def test_explicitly_synthetic_source_stays_synthetic(self):
        inp, cand = adapted_candidate()
        bundle = cand.analysis_projection.event_bundles[0]
        bundle.sources[0].is_synthetic = True
        col = EvidenceCollector(cand, inp.company_context)
        col.add_evidence(bundle.evidence[0].evidence_id)
        self.assertEqual((col.items[0].type, col.items[0].is_synthetic), ("synthetic", True))


class QuoteCoordinatesTests(unittest.TestCase):
    def test_start_end_survive_bridge_and_are_optional_in_json(self):
        inp, cand = adapted_candidate()
        ev = cand.analysis_projection.event_bundles[0].evidence[0]
        self.assertIsInstance(ev.start, int)
        col = EvidenceCollector(cand, inp.company_context)
        col.add_evidence(ev.evidence_id)
        item = col.items[0]
        self.assertEqual((item.start, item.end), (ev.start, ev.end))
        # Без координат (старые пакеты) поля пустые, обязательные поля контракта на месте.
        ev.start = ev.end = None
        col2 = EvidenceCollector(cand, inp.company_context)
        col2.add_evidence(ev.evidence_id)
        dumped = col2.items[0].model_dump(mode="json")
        self.assertIsNone(dumped["start"])
        self.assertEqual({"evidence_id", "type", "title", "publisher", "date", "quote", "is_synthetic"} - set(dumped), set())


class UiLimitationTests(unittest.TestCase):
    def test_english_and_code_limitations_do_not_reach_card(self):
        from app.analyst.qa import Validator
        from app.analyst.view import build_prompt_view

        b = batch()
        b["limitations"] = ["Search similarity is not business significance; final ranking belongs to the analyst",
                            "raw_fallback_may_include_duplicates", "some_unknown_code"]
        p, i = contexts()
        inp = adapt_retrieval_batch(b, p, i, "cfo").analyst_input
        view = build_prompt_view(inp, inp.candidates[0], extras=True)
        self.assertIn("raw_fallback_may_include_duplicates", view.data["event"]["limitations"], "модель видит пакет как есть")
        lims = Validator(view, "cfo").limitations
        self.assertTrue(lims)
        for text in lims:
            self.assertRegex(text, "[а-яё]", f"в карточку попала строка без кириллицы: {text}")
        self.assertNotIn("some_unknown_code", lims)
        self.assertTrue(any(t.startswith("Публикации не объединены") for t in lims))


class BatchShapeTests(unittest.TestCase):
    def test_non_dict_batch_is_validation_error_not_attribute_error(self):
        service, fake, _ = make_service()
        p, i = contexts()
        for bad in ([], "batch", None, 42):
            with self.subTest(bad=bad), self.assertRaises(BatchValidationError) as ctx:
                service.analyze_batch(bad, "cfo", company_profile=p, active_interests=i)
            self.assertEqual(ctx.exception.errors, ["batch_object_required"])
        self.assertEqual(fake.stats.calls, 0)


if __name__ == "__main__":
    unittest.main()
