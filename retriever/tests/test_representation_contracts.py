import json
import unittest
from dataclasses import FrozenInstanceError
from retriever.representation_contracts import *

class ContractTests(unittest.TestCase):
    def test_normalization_and_hash(self):
        self.assertEqual(normalize_embedding_text('  е\u0308\n test\t '), 'ё test')
        self.assertEqual(text_hash('a  b'), text_hash('a b'))
        self.assertEqual(content_hash({'b':2,'a':1}), content_hash({'a':1,'b':2}))
    def test_frozen_and_vector_not_json(self):
        v=EncodedView('c','business','h','k','fp','sha','pv',vector=object())
        self.assertNotIn('vector',v.to_dict())
        json.dumps(v.to_dict())
        with self.assertRaises(FrozenInstanceError): v.kind='other'
    def test_base_partition(self):
        p=CompanyViews('c','v',omitted={k:'missing' for k in BASE_VIEWS})
        self.assertEqual(len(p.to_dict()['omitted']),3)
        with self.assertRaises(ValueError): CompanyViews('c','v')
    def test_policy_and_event_version(self):
        self.assertFalse(GraphPolicy().enabled)
        with self.assertRaises(ValueError): GraphPolicy(weight_policy='random')
        with self.assertRaises(ValueError): SnapshotKey('x','2','h')
    def test_text_checksum(self):
        with self.assertRaises(ValueError):
            CompanyView('c','v','business','text','wrong','h','v')
    def test_error_and_nested_serialization(self):
        e=EncoderMismatch('different models',context={'expected':'cpu'})
        self.assertEqual(e.code,'encoder_mismatch')
        self.assertEqual(BuildConfig('/tmp/x').to_dict()['encoder_config']['model_id'],'intfloat/multilingual-e5-small')
