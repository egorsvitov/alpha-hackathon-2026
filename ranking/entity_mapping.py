"""Explicit trusted resolver configuration, never inferred from news names."""
from datetime import datetime

def validate_entity_mapping(mapping):
    if mapping is None: return None
    if not isinstance(mapping.get('resolver_version'),str) or not mapping['resolver_version']: raise ValueError('resolver_version required')
    seen=set()
    for entry in mapping['entries']:
        if entry.get('status') not in {'verified','synthetic'} or not entry.get('provenance') or not entry.get('news_entity_id') or not entry.get('company_id'): raise ValueError('invalid entity mapping')
        key=(entry['news_entity_id'],entry['company_id'])
        if key in seen: raise ValueError('duplicate mapping')
        seen.add(key)
    return mapping

def resolve(mapping,entity_id,*,as_of,source_mode,is_synthetic):
    if not mapping: return []
    found=[]
    for e in mapping['entries']:
        if e['news_entity_id']!=entity_id: continue
        if e['status']=='synthetic' and not (source_mode=='fixture' and is_synthetic): continue
        # Explicit bounded applicability when provided; missing bounds mean unbounded.
        start=e.get('valid_from');end=e.get('valid_until')
        if start and datetime.fromisoformat(start)>as_of: continue
        if end and datetime.fromisoformat(end)<as_of: continue
        if e.get('scope') not in (None,'all',source_mode): continue
        found.append(e)
    return found
