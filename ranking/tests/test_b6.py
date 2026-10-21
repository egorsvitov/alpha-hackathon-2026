import copy
import json
import tempfile
import unittest
from pathlib import Path
from ranking.b6 import validate_bundle, decode_json, load_bundles
from ranking.contracts import PROJECT_ROOT, InputError, resolve_pointer

class B6Tests(unittest.TestCase):
    def setUp(self):
        self.bundle = json.loads((PROJECT_ROOT/'docs/b6-examples/normal.json').read_text())
    def codes(self,b):
        return {i.code for i in validate_bundle(b) if i.severity == 'error'}
    def test_four_examples(self):
        for p in (PROJECT_ROOT/'docs/b6-examples').glob('*.json'):
            with self.subTest(p=p.name): self.assertFalse(self.codes(json.loads(p.read_text())))
    def test_mutations(self):
        mutations = [
            ('evidence_source_missing',lambda b:b['evidence'][0].update(source_id='absent')),
            ('claim_evidence_missing',lambda b:b['claims'][0].update(evidence_ids=['absent'])),
            ('measurement_range_invalid',lambda b:b['claims'][0]['measurements'][0].update(value={'min':'21','max':'3'})),
            ('time_range_invalid',lambda b:b['claims'][0]['time']['occurred_at'].update(start='2026-10-22')),
            ('duplicate_id',lambda b:b['sources'].append(copy.deepcopy(b['sources'][0]))),
            ('quote_span_invalid',lambda b:b['evidence'][0].update(end=999)),
            ('field_pointer_invalid',lambda b:b['claims'][0]['field_evidence'].update({'/participants/00':b['claims'][0]['evidence_ids']})),
            ('field_evidence_uncovered',lambda b:b['claims'][0]['field_evidence'].pop('/text')),
            ('schema_error',lambda b:b['sources'][0].update(is_synthetic='true'))]
        for code,mutation in mutations:
            with self.subTest(code=code):
                b=copy.deepcopy(self.bundle);mutation(b);self.assertIn(code,self.codes(b))
    def test_source_specific(self):
        b=self.bundle
        second=copy.deepcopy(b['evidence'][0]);second['evidence_id']='second';b['evidence'].append(second);b['claims'][0]['evidence_ids'].append('second')
        self.assertFalse(self.codes(b))
        source=copy.deepcopy(b['sources'][0]);source.update(source_id='other',article_id='other');b['sources'].append(source);second['source_id']='other'
        self.assertIn('claim_cross_source_evidence',self.codes(b))
        claim=copy.deepcopy(b['claims'][0]);claim['claim_id']='other';claim['measurements'][0]['measurement_id']='other';claim['evidence_ids']=['second'];claim['field_evidence']={p:['second'] for p in claim['field_evidence']};b['claims'].append(claim);b['claims'][0]['evidence_ids'].remove('second')
        self.assertFalse(self.codes(b))
    def test_unicode_title(self):
        b=self.bundle;b['sources'][0]['title']='А😀Б';b['evidence'][0].update(field='title',quote='😀',start=1,end=2)
        self.assertFalse(self.codes(b));b['evidence'][0]['end']=3;self.assertIn('quote_span_invalid',self.codes(b))
    def test_pointers(self):
        self.assertEqual(resolve_pointer({'a/b':{'~':[5]}},'/a~1b/~0/0'),5)
        for path in ['/a~2','/a/01','/a/-','/a/-1']:
            with self.assertRaises((ValueError,KeyError)): resolve_pointer({'a':[1]},path)
    def test_decoder(self):
        for text in ['{"a":1,"a":2}','{"a":NaN}','{"a":Infinity}','{"a":1e999}']:
            with self.assertRaises(InputError):decode_json(text)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'events.jsonl';path.write_text('{}\n{bad}\n')
            with self.assertRaisesRegex(InputError,'line 2'):load_bundles(path)
            with self.assertRaises(InputError):load_bundles(path,max_bytes=1)
    def test_versions_and_flags(self):
        for version in ['10',True,0]:
            b=copy.deepcopy(self.bundle);b['event']['event_version']=version;self.assertIn('schema_error',self.codes(b))
        self.bundle['sources'][0]['content_kind']='original'
        self.assertFalse(self.codes(self.bundle));self.assertIn('source_flags_ambiguous',{i.code for i in validate_bundle(self.bundle)})
    def test_open_labels_and_withdrawn(self):
        self.bundle['ranking_features']['factors']=['future_factor'];self.assertFalse(self.codes(self.bundle))
        self.bundle['ranking_features']['topics']=['future_topic'];self.assertIn('schema_error',self.codes(self.bundle))
        self.bundle['ranking_features']['topics']=[];self.bundle['claims']=[];self.assertIn('schema_error',self.codes(self.bundle))
        self.bundle['event']['availability']='withdrawn';self.assertFalse(self.codes(self.bundle))
