import json
import unittest
from llm.event_pipeline import (
    source_blocks,
    packets,
    parse_mentions,
    BM25Retriever,
    EventMention,
    parse_decisions,
    attachment,
)


class CoreTests(unittest.TestCase):
    def document(self, text):
        return {
            "document_id": "d",
            "article": {"title": "technical", "text": text},
            "provenance": [{"title_origin": "generated_identifier"}],
        }

    def test_unicode_blocks_and_long_text(self):
        d = self.document("😀 Цена\u00a014%.\n\n" + ("Очень длинный текст. " * 200))
        bs = source_blocks(d, 70)
        for b in bs:
            self.assertEqual(b.text, d["article"][b.field][b.start : b.end])
            self.assertLessEqual(len(b.text), 70)
            self.assertEqual(b.field, "text")
        self.assertEqual(
            {b.block_id for b in bs},
            {bid for p in packets(bs, 100) for bid in p["primary"]},
        )

    def test_permissive_hints_and_bad_record(self):
        bs = source_blocks(self.document("Инвестиции выросли на 4 б/с."))
        p = next(packets(bs))
        bid = bs[0].block_id
        result = parse_mentions(
            json.dumps(
                {
                    "mentions": [
                        {"block_ids": [bid], "gloss": 7, "hints": {"time": None}},
                        {"block_ids": ["fake"]},
                        {"block_ids": [bid], "gloss": "Другое событие"},
                    ]
                }
            ),
            "d",
            p,
        )
        self.assertEqual(len(result.mentions), 2)
        self.assertEqual(len(result.rejected), 1)
        self.assertIsNone(result.mentions[0].gloss)
        self.assertTrue(result.mentions[0].diagnostics)

    def test_empty_and_invalid_json(self):
        p = next(packets(source_blocks(self.document("Текст"))))
        self.assertEqual(
            parse_mentions('```json\n{"mentions":[]}\n```', "d", p).mentions, []
        )
        with self.assertRaises(ValueError):
            parse_mentions("{", "d", p)

    def test_numeric_conflict_retrieval(self):
        a = source_blocks(self.document("ЦБ сохранил ставку 14%"))[0]
        d = self.document("ЦБ сохранил ставку 15%")
        d["document_id"] = "e"
        b = source_blocks(d)[0]
        m = EventMention(mention_id="m", document_id="d", block_ids=[a.block_id])
        n = EventMention(mention_id="n", document_id="e", block_ids=[b.block_id])
        result = BM25Retriever().search(
            n, [m], {a.block_id: a, b.block_id: b}, {"m": "c"}
        )
        self.assertEqual(result["cluster_ids"], ["c"])

    def test_decision_completeness_and_uncertainty(self):
        d = {
            "cluster_id": "c",
            "identity": "same",
            "claim_compatibility": "conflicting",
            "mention_ids": ["m"],
            "block_ids": ["b"],
            "reason": "Один выпуск",
        }
        parsed = parse_decisions(json.dumps({"decisions": [d]}), ["c"], ["m"], ["b"])
        self.assertEqual(attachment(parsed), "c")
        d2 = dict(d, cluster_id="c2", identity="uncertain")
        parsed = parse_decisions(
            json.dumps({"decisions": [d, d2]}), ["c", "c2"], ["m"], ["b"]
        )
        self.assertIsNone(attachment(parsed))
        with self.assertRaises(ValueError):
            parse_decisions(json.dumps({"decisions": [d]}), ["c", "c2"], ["m"], ["b"])


if __name__ == "__main__":
    unittest.main()


class IdentityFixtureTests(unittest.TestCase):
    def test_all_manual_identity_cases_preserve_quotes_and_decisions(self):
        from pathlib import Path

        cases = json.loads(
            Path(__file__)
            .parents[1]
            .joinpath("fixtures/identity_cases.json")
            .read_text()
        )
        for c in cases:
            with self.subTest(c["id"]):
                docs = [
                    {
                        "document_id": str(i),
                        "article": {"title": "", "text": c[k]},
                        "provenance": [],
                    }
                    for i, k in enumerate(("a", "b"))
                ]
                blocks = [source_blocks(d)[0] for d in docs]
                for b, k in zip(blocks, ("a", "b")):
                    self.assertEqual(b.text, c[k])
                value = {
                    "cluster_id": "c",
                    "identity": c["identity"],
                    "claim_compatibility": c["compatibility"],
                    "mention_ids": ["old", "new"],
                    "block_ids": [b.block_id for b in blocks],
                    "reason": "Manually authored expected relation; scripted test, not model evaluation.",
                }
                decisions = parse_decisions(
                    json.dumps({"decisions": [value]}),
                    ["c"],
                    ["old", "new"],
                    [b.block_id for b in blocks],
                )
                self.assertEqual(
                    attachment(decisions), "c" if c["identity"] == "same" else None
                )
