"""Opt-in local artifact check; never downloads models in unit tests."""
import os
from pathlib import Path
import unittest
import numpy as np
from retriever.embeddings import Encoder
from retriever.representation_contracts import EncoderConfig,InputTooLong

@unittest.skipUnless(os.environ.get('RETRIEVER_REAL_CPU_TEST')=='1','local real CPU smoke is opt-in')
class EncoderRealTests(unittest.TestCase):
    def test_shapes_repeat_prefix_padding_and_overflow(self):
        e=Encoder(EncoderConfig(model_dir='ranking/experiments/cpu_news_embeddings/artifacts/model'))
        self.assertEqual(e.session.get_providers(),['CPUExecutionProvider'])
        texts=['Строительство жилых зданий.','Компания использует кредиты.','Регион регистрации: Москва.']
        q=e.encode_queries(texts);p=e.encode_documents(texts)
        self.assertEqual(q.shape,(3,384));self.assertEqual(q.dtype,np.float32);np.testing.assert_allclose(np.linalg.norm(q,axis=1),1,atol=1e-5)
        np.testing.assert_array_equal(q,e.encode_queries(texts))
        self.assertFalse(np.array_equal(q,p))
        self.assertEqual(e.encode_queries([]).shape,(0,384))
        for text in ['', 'query: x','passage: x']:
            with self.assertRaises(ValueError):e.encode_queries([text])
        with self.assertRaises(InputTooLong):e.encode_documents(['компания '*2000])
