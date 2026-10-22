import hashlib
import unittest
from dataclasses import replace
from llm.agent.source_registry import build_source_registry
from llm.agent.tests.test_context import fixture_inputs

class SourcesTests(unittest.TestCase):
    def test_ids_immutable_quotes_and_original_refs(self):
        s,*_=fixture_inputs();a=build_source_registry(s);b=build_source_registry(s)
        self.assertEqual(a,b)
        row=next(iter(a.values()))
        self.assertTrue(row['source_id'].startswith('news:'))
        self.assertEqual(row['text_sha256'],hashlib.sha256(row['quote'].encode()).hexdigest())
        row['quote']='mutated'
        self.assertNotEqual(a,build_source_registry(s))
    def test_version_ids_change(self):
        s,*_=fixture_inputs()
        self.assertNotEqual(set(build_source_registry(s)),set(build_source_registry(replace(s,event_version=2))))
