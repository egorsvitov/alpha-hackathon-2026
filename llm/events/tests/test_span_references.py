"""Code-owned evidence boundaries; all tests run without keys or network."""
from copy import deepcopy
import json
import unittest
from jsonschema import Draft202012Validator
from llm.events.tests import test_extraction_v2 as fixtures
from llm.events.tests.test_extraction_v2 import DISCOVERY, observation, response
from llm.events.extraction import staged as core, staged_models as m
from llm.events.extraction.references import fragments_with_spans, locate_span, wire_schema
from llm.events.extraction.service import StageRunner
from llm.events.extraction.provider import OfflineProvider
from llm.events.storage.store import StorageError


def ref(fragment='P1', start=1, end=None):
    return {'fragment_id':fragment,'start_id':f'{fragment}.S{start}',
            'end_id':f'{fragment}.S{end or start}'}


def convert(value):
    if isinstance(value,list): return [convert(x) for x in value]
    if not isinstance(value,dict): return value
    if set(value)=={'fragment_id','quote'}: return ref(value['fragment_id'])
    return {k:convert(v) for k,v in value.items()}


class SpanTests(unittest.TestCase):
    setUp = fixtures.V2Tests.setUp
    service = fixtures.V2Tests.service

    def discover(self,data):
        spec=StageRunner(self.store,OfflineProvider([]),stage='discover').build_spec(self.profile)
        return core.parse_discovery(json.dumps(data),self.record,spec)

    def with_text(self,text):
        article=self.record.article.model_copy(update={'text':text})
        return self.record.model_copy(update={'article':article})

    def test_unicode_decimal_abbreviation_and_exact_range(self):
        record=self.with_text('😀 Цена 1,1 млн руб. за год. Затем 3,75 копейки!\u00a0Новый итог.\n\nДалее.')
        fragments=fragments_with_spans(record)
        p=fragments[1]
        self.assertEqual(len(p['spans']),3)
        self.assertEqual(p['spans'][0]['text'],'😀 Цена 1,1 млн руб. за год.')
        span=locate_span(m.SpanRef(**ref(start=1,end=2)),record)
        self.assertEqual(record.article.text[span['start']:span['end']],span['quote'])
        self.assertEqual(span['quote'],'😀 Цена 1,1 млн руб. за год. Затем 3,75 копейки!')
        self.assertEqual(fragments,fragments_with_spans(record))

    def test_repeated_sentences_have_distinct_boundaries(self):
        record=self.with_text('Итог 10 рублей. Итог 10 рублей.')
        first=locate_span(m.SpanRef(**ref()),record)
        second=locate_span(m.SpanRef(**ref(start=2)),record)
        self.assertEqual(first['quote'],second['quote'])
        self.assertNotEqual(first['start'],second['start'])
        for span in (first,second):
            e=core._unique_evidence(span,record)
            self.assertEqual(record.article.text.count(e['quote']),1)
            self.assertIn(span['quote'],e['quote'])

    def test_unknown_reversed_cross_fragment_and_extra_fields(self):
        record=self.with_text('Первое. Второе.')
        for data,code in [(ref(start=9),'unknown_span'),(ref(start=2,end=1),'invalid_span_order'),
                          ({**ref(),'end_id':'P2.S1'},'unknown_span')]:
            with self.assertRaises(StorageError) as err: locate_span(m.SpanRef(**data),record)
            self.assertEqual(err.exception.code,code)
        with self.assertRaises(ValueError): m.SpanRef.model_validate({**ref(),'quote':'invented'})

    def test_tables_keep_rows_headers_and_units(self):
        record=self.with_text('Тысяч баррелей в сутки\n| Страна | Сентябрь | Август |\n| РФ | 100 | 90 |')
        p=fragments_with_spans(record)[1]
        self.assertEqual(len(p['spans']),3)
        self.assertEqual(p['spans'][2]['text'],'| РФ | 100 | 90 |')
        self.assertIn('Тысяч баррелей',locate_span(m.SpanRef(**ref(end=3)),record)['quote'])

    def test_discovery_isolates_bad_mentions_and_preserves_ids(self):
        good=convert(DISCOVERY)['mentions'][0]
        bad={**good,'trigger':ref(start=99)}
        result=self.discover({'mentions':[bad,good,{'bad':'schema'}],'limitations':[]})
        self.assertEqual([x.mention_id for x in result.mentions],['d2'])
        self.assertEqual([x.mention_id for x in result.rejected_mentions],['d1','d3'])
        self.assertEqual(result.rejected_mentions[0].issues[0].code,'unknown_span')

    def test_wire_schema_only_ids_and_code_owned_text(self):
        request=core.request_for('discover',self.record,self.profile)
        body=json.loads(request['messages'][1]['content'].split('\n',1)[1])
        self.assertEqual(body['reference_format'],'span-ids.v1')
        self.assertEqual(body['fragments'][1]['spans'][0]['text'],self.record.article.text.split('\n\n')[0])
        schema=wire_schema(m.DiscoveryResponse.model_json_schema())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(convert(DISCOVERY))
        with self.assertRaises(Exception): Draft202012Validator(schema).validate(DISCOVERY)
        # Historical quote-based artifacts still parse.
        self.assertEqual(len(self.discover(DISCOVERY).mentions),1)

    def test_offline_pipeline_partial_audit_cache_and_legacy_grounding(self):
        good=convert(DISCOVERY)['mentions'][0]
        bad={**good,'trigger':ref(start=99)}
        svc=self.service([response({'mentions':[good,bad],'limitations':[]}),
                          response({'records':[convert(observation())],'excluded_mentions':[],'limitations':[]})])
        discovery=svc.discover(self.record.source_id)
        plan=svc.plan_extraction(discovery['artifact_id'])
        batches=svc.run_extraction(plan['artifact_id'],max_jobs=1)
        ids=[x['artifact_id'] for x in batches if x.get('artifact_id')]
        assembly=svc.assemble_variant(plan['artifact_id'],ids)
        self.assertTrue(assembly['incomplete'])
        self.assertEqual(assembly['candidate_count'],1)
        norm=svc.normalize_variant(assembly)
        self.assertEqual(norm['status'],'succeeded')
        payload=self.store.get_artifact(norm['artifact_id']).payload
        self.assertEqual(payload['quality'],'partial')
        self.assertEqual(len(payload['accepted']),1)
        for e in payload['evidence']:
            self.assertEqual(getattr(self.record.article,e['field'])[e['start']:e['end']],e['quote'])
        publication=svc.publish_variant('A',[norm['artifact_id']],experiment_id='span-ids-test')
        self.assertEqual(len(publication['bundles']),1)
        self.assertEqual(publication['bundles'][0]['event']['quality'],'partial')
        self.assertEqual(svc.run_extraction(plan['artifact_id'],max_jobs=1)[0]['status'],'cached')

    def test_semantic_verification_uses_same_id_references(self):
        normalized=fixtures.V2Tests.prepare_a(self)
        verdict=convert(fixtures.V2Tests.verdict(self,normalized))
        result=core.parse_verification(json.dumps(verdict),self.record,'normalized',normalized['accepted'])
        self.assertTrue(all(s.accepted for s in result.selections))
        verdict['verdicts'][0]['fields'][0]['evidence']=[ref(start=99)]
        result=core.parse_verification(json.dumps(verdict),self.record,'normalized',normalized['accepted'])
        self.assertFalse(result.selections[0].accepted)

    def test_bad_record_does_not_discard_other_record(self):
        discovery=self.discover(convert(DISCOVERY))
        plan=core.plan_extraction(self.record,discovery,'d',self.profile,'c')
        bad=convert(observation());bad['subject']['evidence']=[ref(start=99)]
        result=core.compile_batch(json.dumps({'records':[bad,convert(observation())],
                 'excluded_mentions':[],'limitations':[]}),self.record,discovery,plan.batches[0])
        self.assertEqual(len(result['extraction']['claims']),1)
        self.assertEqual(result['issues'][0]['code'],'unknown_span')

if __name__=='__main__': unittest.main()
