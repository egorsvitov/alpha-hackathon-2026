"""Research checks for B6 fixtures; not a complete runtime validator.

Run: uv run --no-project --with jsonschema==4.25.1 python scripts/review_event_bundle.py
"""
import copy,json
from pathlib import Path
from datetime import date,datetime
from decimal import Decimal
from importlib.metadata import version
from jsonschema import Draft202012Validator,FormatChecker
root=Path(__file__).resolve().parents[1]
schema=json.loads((root/'docs/contracts/event-bundle.schema.json').read_text())
Draft202012Validator.check_schema(schema)
validator=Draft202012Validator(schema,format_checker=FormatChecker())
def pointer(obj,path):
    for token in path.split('/')[1:]:
        token=token.replace('~1','/').replace('~0','~')
        obj=obj[int(token)] if isinstance(obj,list) else obj[token]
    return obj
def check(d):
    errors=[]
    indexes={}
    for group,key in [('claims','claim_id'),('sources','source_id'),('evidence','evidence_id'),('conflicts','conflict_id')]:
        indexes[group]={x[key]:x for x in d[group]}
        if len(indexes[group])!=len(d[group]): errors.append(f'duplicate {key}')
    evidence=indexes['evidence']; sources=indexes['sources']; claims=indexes['claims']
    measurement_ids=[]
    for e in evidence.values():
        if e['source_id'] not in sources: errors.append('evidence source missing')
        if not e['start']<e['end'] or len(e['quote'])!=e['end']-e['start']: errors.append('invalid quote span')
        source=sources.get(e['source_id'])
        if source and e['field']=='title' and source['title'][e['start']:e['end']]!=e['quote']: errors.append('title quote mismatch')
    for c in claims.values():
        if any(e not in evidence for e in c['evidence_ids']): errors.append('claim evidence missing')
        source_ids={evidence[e]['source_id'] for e in c['evidence_ids'] if e in evidence}
        if len(source_ids)!=1: errors.append('claim spans multiple/no sources')
        for path,ids in c['field_evidence'].items():
            try: pointer(c,path)
            except (ValueError,KeyError,IndexError,TypeError): errors.append('field pointer invalid')
            if not set(ids)<=set(c['evidence_ids']): errors.append('field evidence outside claim evidence')
        periods=list(c['time'].values())+[m['comparison_period'] for m in c['measurements']]
        for period in periods:
            if period and period['start'] and period['end'] and date.fromisoformat(period['start'])>date.fromisoformat(period['end']): errors.append('period reversed')
        for m in c['measurements']:
            measurement_ids.append(m['measurement_id'])
            if isinstance(m['value'],dict) and Decimal(m['value']['min'])>Decimal(m['value']['max']): errors.append('measurement range reversed')
    if len(set(measurement_ids))!=len(measurement_ids): errors.append('duplicate measurement_id')
    for conflict in d['conflicts']:
        for variant in conflict['variants']:
            try: pointer(claims[variant['claim_id']],variant['field_path'])
            except (ValueError,KeyError,IndexError,TypeError): errors.append('conflict pointer invalid')
    for r in d['relations']:
        if r['target_kind']=='claim':
            if r['from_id'] not in claims: errors.append('relation origin missing')
            if r['type']=='duplicate_claim' and r['to_id'] not in claims: errors.append('duplicate target missing')
    dates=[datetime.fromisoformat(s['published_at']) for s in d['sources'] if s['published_at']]
    first=d['event']['first_published_at']
    if bool(dates)!=(first is not None) or (dates and min(dates)!=datetime.fromisoformat(first)): errors.append('earliest publication mismatch')
    for s in d['sources']:
        if (s['content_kind']=='synthetic')!=s['is_synthetic']: errors.append('synthetic metadata inconsistent')
    return errors
print('jsonschema',version('jsonschema'))
for path in sorted((root/'docs/b6-examples').glob('*.json')):
    d=json.loads(path.read_text()); schema_errors=list(validator.iter_errors(d)); semantic_errors=check(d)
    print(path.name,'schema_errors=',len(schema_errors),'structural_semantic_errors=',semantic_errors)
    assert not schema_errors and not semantic_errors
normal=json.loads((root/'docs/b6-examples/normal.json').read_text())
mutations={}
a=copy.deepcopy(normal);a['claims'][0]['evidence_ids']=['missing'];mutations['dangling evidence']=a
a=copy.deepcopy(normal);a['evidence'][0]['end']=999;mutations['bad quote offsets']=a
a=copy.deepcopy(normal);a['claims'][0]['measurements'][0]['value']={'min':'21','max':'20'};mutations['reversed numeric range']=a
a=copy.deepcopy(normal);a['claims'][0]['time']['occurred_at']['start']='2026-10-22';mutations['reversed date interval']=a
a=copy.deepcopy(normal);a['event']['first_published_at']='2026-10-19T00:00:00+03:00';mutations['wrong first publication']=a
for name,d in mutations.items():
    schema_errors=list(validator.iter_errors(d)); semantic_errors=check(d)
    print('NEGATIVE',name,'schema_errors=',len(schema_errors),'structural_semantic_errors=',semantic_errors)
    assert not schema_errors and semantic_errors
print('NOTE: original article texts absent; full quote equality and meaning were not independently verified.')
