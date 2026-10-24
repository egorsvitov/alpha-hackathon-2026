"""Explicitly prepare a minimal verified OKVED profile; never invent facts.

Run once on the private prepare_reference output, before the worker.
The complete parent is retained privately and validated by the analyst adapter.
"""
import argparse
import json
from pathlib import Path

from app.analyst.retrieval_adapter import _company
from retriever.models import digest
from .ingest import atomic_json


def project(root, company_id):
    root = Path(root)
    manifest = json.loads((root/'manifest.json').read_text())
    entry = next(e for e in manifest['companies'] if e['company_id'] == company_id)
    path = root/entry['profile']
    parent = json.loads(path.read_text())
    if 'trusted_parent_profile_version' in parent:
        return parent['profile_version']
    assert digest(parent) == entry['profile_sha256'], 'parent hash mismatch'
    assert parent.get('is_synthetic') is False, 'verified real parent required'
    _company(parent, parent)
    facts = {k:v for k,v in parent['features'].items()
             if v['path'] in ('/okved/code','/okved/name') and v.get('quality') == 'observed' and v.get('value') is not None}
    assert len(facts) == 2, 'two observed OKVED facts required'
    profile = dict(company_id=company_id, is_synthetic=False, features=facts,
                   trusted_parent_profile_version=parent['profile_version'],
                   limitations=['Проверка ограничена наблюдаемыми признаками ОКВЭД; остальные признаки не переданы.'])
    profile['profile_version'] = digest(profile)
    _company(profile, profile)
    atomic_json(root/(company_id+'-trusted-parent.json'), parent)
    interests = json.loads((root/entry['interests']).read_text())
    interests['profile_version'] = profile['profile_version']
    interests['interest_set_version'] = digest([interests['interest_set_version'],profile['profile_version']])
    entry.update(profile_sha256=digest(profile), interests_sha256=digest(interests))
    entry['display'].update(name='Компания '+company_id, profile_version=profile['profile_version'])
    atomic_json(path, profile)
    atomic_json(root/entry['interests'], interests)
    atomic_json(root/'manifest.json', manifest)
    return profile['profile_version']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles', required=True)
    parser.add_argument('--company', required=True)
    args = parser.parse_args()
    print(project(args.profiles, args.company))
