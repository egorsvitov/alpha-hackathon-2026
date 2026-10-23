"""Claim-local, conflict-aware executable mapping rules. No text inference."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import unicodedata
from pathlib import Path

GROUPS = {'industry','product','geography','entity','counterparty','mechanism','macro','lexical'}
PATHS = {'/products/loan','/products/deposit','/products/leasing','/okved/code','/industry','/employees_band','/fts_present'}

def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC',str(value)).casefold().replace('ё','е').split())

def pointer(obj, path):
    for part in path.lstrip('/').split('/'):
        obj = obj[int(part)] if isinstance(obj,list) else obj[part.replace('~1','/').replace('~0','~')]
    return obj

def feature(profile,path):
    return next((f for f in profile.get('features',{}).values() if f['path']==path),None)

def known_feature(profile,path):
    f=feature(profile,path)
    return f is not None and f.get('quality') not in {'unknown','conflicting'}

def validate_mappings(config):
    def expr(e):
        if set(e) in ({'any_of'},{'all_of'}):
            values=next(iter(e.values()))
            if not isinstance(values,list) or not values: raise ValueError('empty expression')
            for v in values: expr(v)
        else:
            if e.get('path') not in PATHS or e.get('op') not in {'equals','prefix','known'}: raise ValueError('unknown company selector')
            if e['op']=='known' and 'value' in e: raise ValueError('known has no value')
            if e['op']!='known' and 'value' not in e: raise ValueError('missing value')
            if e['op']=='prefix' and (e['path']!='/okved/code' or not isinstance(e['value'],str)): raise ValueError('invalid prefix')
            if set(e)-{'path','op','value'}: raise ValueError('unknown expression key')
    ids=set()
    for r in config['rules']:
        if r['rule_id'] in ids: raise ValueError('duplicate rule_id')
        ids.add(r['rule_id']);expr(r['company_match'])
        if r['geography_policy'] not in {'ru_or_unknown','country_overlap','currency_unconfirmed'}: raise ValueError('unknown geography policy')
        if not r['event_match']['event_types'] or not r['event_match']['claim_any_of']: raise ValueError('empty event selector')
        for s in r['event_match']['claim_any_of']:
            if s['path'] not in {'/object','/measurements/*/metric'} or not s['aliases'] or any(not isinstance(a,str) for a in s['aliases']): raise ValueError('unknown claim selector')
        for a in r['awards']:
            if a['group'] not in GROUPS or isinstance(a['points'],bool) or not isinstance(a['points'],(int,float)) or not 0<=a['points']<float('inf'): raise ValueError('invalid award')
    return config

def load_mappings(path=None):
    return validate_mappings(json.loads(Path(path or Path(__file__).parent/'config/event-mappings.json').read_text()))

def company_match(e,profile):
    if 'any_of' in e or 'all_of' in e:
        key='any_of' if 'any_of' in e else 'all_of'
        results=[company_match(x,profile) for x in e[key]]
        ok=any(x[0] for x in results) if key=='any_of' else all(x[0] for x in results)
        return ok, sorted({f for passed,fs in results if passed for f in fs}) if ok else []
    path=e['path'];f=feature(profile,path)
    if not known_feature(profile,path): return False,[]
    v=pointer(profile,path); op=e['op']
    ok=(v is not None and v!='') if op=='known' else ((isinstance(v,str) and (v==e['value'] or v.startswith(e['value']+'.'))) if op=='prefix' else type(v)==type(e['value']) and v==e['value'])
    return ok,[f['feature_id']] if ok else []

def prepare_signals(snapshot):
    b=snapshot.bundle
    disputed={(v['claim_id'],v['field_path']) for c in b.get('conflicts',[]) for v in c['variants']}
    return {'snapshot':snapshot,'claims':copy.deepcopy(b['claims']),'ranking_features':copy.deepcopy(b['ranking_features']),'disputed':disputed,'limitations':list(b.get('limitations',[]))}

def disputed(signals,claim_id,path):
    return any(cid==claim_id and (p=='' or path==p or path.startswith(p+'/') or p.startswith(path+'/')) for cid,p in signals['disputed'])

def mapping_candidates(signals,profile,mappings=None):
    mappings=mappings or load_mappings(); b=signals['snapshot'].bundle
    awards=[]; diagnostics=[]
    for rule in mappings['rules']:
        if b['event']['event_type'] not in rule['event_match']['event_types']: continue
        ok,features=company_match(rule['company_match'],profile)
        if not ok: continue
        for index,c in enumerate(signals['claims']):
            cid=c['claim_id']; paths=[]
            for sel in rule['event_match']['claim_any_of']:
                candidates=[('/object',c.get('object'))] if sel['path']=='/object' else [(f'/measurements/{i}/metric',m['metric']) for i,m in enumerate(c.get('measurements',[]))]
                for path,value in candidates:
                    if normalize(value) in {normalize(a) for a in sel['aliases']} and not disputed(signals,cid,path) and not disputed(signals,cid,'/object'): paths.append(path)
            if not paths: continue
            limits=[]; fs=list(features); geo=c.get('geographies',[]); policy=rule['geography_policy']; country_ok=known_feature(profile,'/countries'); countries=profile.get('countries',[])
            if policy=='ru_or_unknown':
                if disputed(signals,cid,'/geographies') or (geo and 'RU' not in geo): continue
                if not geo: limits.append('event_geography_unknown')
                limits.append('operating_territory_unknown')
            elif policy=='country_overlap':
                if not country_ok or not countries or not set(countries)&set(geo) or disputed(signals,cid,'/geographies'): continue
                fs.append(feature(profile,'/countries')['feature_id']); paths.append('/geographies');limits.append('trade_share_and_direction_unknown')
            else:
                if not country_ok or not countries: continue
                fs.append(feature(profile,'/countries')['feature_id']); limits.append('contract_currency_unknown')
            diagnostic={'rule_id':rule['rule_id'],'claim_id':cid,'modality':c['modality'],'polarity':c['polarity'],'stage':c.get('stage'),'action':c.get('action'),'limitations':limits}
            diagnostics.append(diagnostic)
            if c['polarity']!='affirmed' or c['modality'] not in {'asserted','forecast','plan','proposal'} or any(disputed(signals,cid,p) for p in ['/modality','/polarity']): continue
            if any(x==cid for x,_ in signals['disputed']): limits.append('conflicting_claim_fields_no_numeric_inference')
            if c['modality']!='asserted': limits.append('modality_'+c['modality'])
            for award in rule['awards']:
                awards.append({**award,'rule_id':rule['rule_id'],'claim_id':cid,'feature_ids':sorted(set(fs)),'event_paths':[f'/claims/{index}'+p for p in sorted(set(paths))]+[f'/claims/{index}/modality',f'/claims/{index}/polarity',f'/claims/{index}/stage',f'/claims/{index}/action'],'basis':'metadata_match' if award['group']=='macro' else 'conditional_mechanism','limitations':sorted(set(limits)),'text':f"{award['mechanism']}; modality={c['modality']}; polarity={c['polarity']}; action={c.get('action')}; stage={c.get('stage')}. Экспозиция и финансовый эффект не установлены."})
    for rule in mappings['rules']:
        if b['event']['event_type'] not in rule['event_match']['event_types']: continue
        applicable, fs = company_match(rule['company_match'], profile)
        if not applicable: continue
        aliases = {normalize(a) for selector in rule['event_match']['claim_any_of'] for a in selector['aliases']}
        paths = [f'/ranking_features/{key}/{i}' for key in ('objects', 'metrics') for i, value in enumerate(b['ranking_features'].get(key, [])) if normalize(value) in aliases]
        if paths:
            diagnostics.append({'rule_id': rule['rule_id'], 'basis': 'metadata_match', 'event_paths': paths, 'feature_ids': fs, 'claim_ids': [], 'awards': [], 'limitations': ['metadata_only_no_claim_awards']})
    return awards,diagnostics
