import csv
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np

from retriever.experiments.activity_catalogue import (
    source_inventory, validate_catalogue, query_text, compile_catalogue,
)
from retriever.experiments.activity_index import build_activity_index, ActivityIndex
from retriever.representation_contracts import EncoderMismatch, CorruptVector, ChunkRef, SnapshotKey


def record():
    return dict(okved_code='62.01', activity='Разработка программного обеспечения',
        search_description='Возможные темы: уязвимости программ и безопасность разработки.',
        financial_search_description='Возможные затраты: оплата разработчиков и лицензии на инструменты.',
        conditional_topics=[dict(topic='PHP', condition='Только при подтверждённом использовании PHP.')],
        status='hypothesis', generator_model='gpt-6-luna')


class FakeEncoder:
    fingerprint = 'test-only-fingerprint'
    fingerprint_metadata = {'test_only': True}

    def encode_queries(self, texts):
        result = np.zeros((len(texts), 384), dtype=np.float32)
        for i in range(len(texts)):
            result[i, i % 384] = 1
        return result


class ActivityTests(unittest.TestCase):
    def source(self, root):
        path = root / 'companies.csv'
        with path.open('w', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(['SYNTHETIC_U', 'MAINOKVED_CODE', 'MAINOKVED_NAME', 'REGION'])
            writer.writerow(['a', '62.01', record()['activity'], 'Москва'])
            writer.writerow(['a', '62.01', record()['activity'], 'Москва'])
            writer.writerow(['conflict', '62.01', record()['activity'], 'Москва'])
            writer.writerow(['conflict', '62.01', record()['activity'], 'Пермь'])
        return path

    def catalogue(self, root):
        source = self.source(root)
        shard = root / 'shard.json'
        shard.write_text(json.dumps([record()], ensure_ascii=False))
        compile_catalogue(source, [shard], root / 'catalogue')
        return root / 'catalogue'

    def test_source_deduplicates_identical_rows_and_excludes_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            activities, mapping, excluded = source_inventory(self.source(Path(tmp)))
            self.assertEqual(mapping, {'a': '62.01'})
            self.assertEqual(excluded, ['conflict'])
            self.assertEqual(activities[0]['company_rows'], 4)

    def test_requires_exact_coverage_and_labels(self):
        expected = [record()]
        for rows in ([], [record(), record()], [dict(record(), activity='Неверное имя')],
                     [dict(record(), status='observed')]):
            with self.assertRaises(ValueError):
                validate_catalogue(rows, expected)

    def test_conditions_never_become_unconditional_search_text(self):
        for kind in ('activity_topics', 'activity_finance'):
            self.assertNotIn('PHP', query_text(record(), kind))
            self.assertIn('Возможные темы', query_text(record(), kind))

    def test_catalogue_tampering_rejected_before_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cat = self.catalogue(root)
            (cat / 'catalogue.json').write_text('[]')
            with self.assertRaises(CorruptVector):
                build_activity_index(cat, root / 'index', encoder=FakeEncoder())

    def test_roundtrip_company_lookup_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idx = build_activity_index(self.catalogue(root), root / 'index', encoder=FakeEncoder())
            self.assertEqual(len(idx.queries('a')), 2)
            self.assertEqual(idx.queries('a')[0][0]['status'], 'hypothesis')
            with self.assertRaises(KeyError):
                idx.queries('conflict')
            with self.assertRaises(EncoderMismatch):
                idx.search_company('a', SimpleNamespace(fingerprint='old-int8'))
            with (root / 'index' / 'vectors.npy').open('ab') as f:
                f.write(b'corrupted')
            with self.assertRaises(CorruptVector):
                ActivityIndex.open(root / 'index')

    def test_search_deduplicates_windows_and_preserves_hypothesis_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            idx = build_activity_index(self.catalogue(root), root / 'index', encoder=FakeEncoder())
            chunks = [ChunkRef(SnapshotKey(eid, 1, 'hash'), str(i), [], [])
                      for i, eid in enumerate(['event-a', 'event-a', 'event-b'])]
            matrix = np.zeros((3, 384), np.float32)
            matrix[0, 0] = 1; matrix[1, 1] = 1; matrix[2, 2] = 1
            events = SimpleNamespace(fingerprint=idx.fingerprint, chunks=chunks, matrix=matrix,
                snapshots={eid: SimpleNamespace(bundle={'event': {'title': eid}})
                           for eid in ['event-a', 'event-b']})
            hits = idx.search_company('a', events)
            self.assertEqual([h['event_id'] for h in hits], ['event-a', 'event-b'])
            self.assertEqual(hits[0]['match']['status'], 'hypothesis')
            self.assertEqual(hits[0]['match']['okved_code'], '62.01')


if __name__ == '__main__':
    unittest.main()
