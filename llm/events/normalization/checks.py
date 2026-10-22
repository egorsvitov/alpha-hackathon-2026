"""Offline B4 fixture checks, never an evaluation of real B3 extraction."""
import json
from pathlib import Path
from llm.events.contracts.models import ArticleInput
from llm.events.normalization.core import normalize_extraction
from llm.events.storage.store import StorageError
from llm.events.contracts.validation import article_sha256


def check_inputs(path,articles_path=None):
    document=json.loads(Path(path).read_text())
    cases=document['cases'] if isinstance(document,dict) else document
    if articles_path is not None:
        corpus=[ArticleInput.model_validate(a) for a in json.loads(Path(articles_path).read_text())]
        expected={a.article_id:article_sha256(a) for a in corpus}
        actual={c['article']['article']['article_id']:c['input']['article_sha256'] for c in cases}
        if len(expected)!=len(corpus) or len(actual)!=len(cases) or actual!=expected:
            raise StorageError('development_corpus_mismatch','One matching hashed input per supplied article is required')
    reports=[]
    for case in cases:
        result=normalize_extraction(case['input'],case['article'])
        expected=case.get('expected',{})
        values=[m['value'] for a in result.model_dump(mode='json')['accepted'] for m in a['claim']['measurements']]
        failures=[]
        expected_values=case.get('expected_values',expected.get('values'))
        if expected_values is not None and values!=expected_values:failures.append('values')
        if 'units' in expected and [m.unit for a in result.accepted for m in a.claim.measurements]!=expected['units']:failures.append('units')
        if 'accepted' in expected and len(result.accepted)!=expected['accepted']:failures.append('accepted')
        if 'quality' in expected and result.quality!=expected['quality']:failures.append('quality')
        if 'rejected' in expected and expected['rejected'] not in {i.code for r in result.rejected for i in r.issues}:failures.append('rejected')
        if 'warning' in expected and expected['warning'] not in {d.issue.code for d in result.diagnostics}:failures.append('warning')
        if 'period_start' in expected and (not result.accepted or result.accepted[0].claim.time.observation_period.start!=expected['period_start']):failures.append('period_start')
        if 'entity_id' in expected and (not result.accepted or result.accepted[0].claim.participants[0].entity_id!=expected['entity_id']):failures.append('entity_id')
        reports.append({'article_id':result.source.article_id,'quality':result.quality,'accepted':len(result.accepted),'rejected':len(result.rejected),'failed_checks':failures})
    if any(r['failed_checks'] for r in reports):raise StorageError('normalization_fixture_mismatch',json.dumps(reports,ensure_ascii=False))
    return {'status':'passed','cases':len(cases),'report':reports,'note':'Hand-authored normalization inputs; not real extraction quality or factuality assessment.'}
