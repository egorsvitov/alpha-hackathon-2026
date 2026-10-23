"""Frozen, checksum-verified evaluation inputs and explicitly unlabelled forms."""
from dataclasses import dataclass
from pathlib import Path
import json
from jsonschema import Draft202012Validator
from ranking.contracts import payload_hash
from ranking.profiles import file_hash
from ...representation_contracts import content_hash

@dataclass(frozen=True)
class EvaluationDataset:
    manifest: dict
    profiles: dict
    events: dict
    queries: list
    judgments: list
    dataset_hash: str
    payment_contexts: dict

def load_dataset(manifest_path):
    path=Path(manifest_path);manifest=json.loads(path.read_text());data={}
    for name,ref in manifest['files'].items():
        item=path.parent/ref['path']
        if file_hash(item)!=ref['sha256']:raise ValueError('dataset checksum mismatch: '+name)
        data[name]=json.loads(item.read_text())
    profiles={p['company_id']:p for p in data['profiles']};events={e['event']['event_id']:e for e in data['events']}
    if len(events)!=len(data['events']):raise ValueError('multiple event snapshots')
    groups={}
    for split,ids in manifest['splits'].items():
        for uid in ids:
            if uid not in events:raise ValueError('unknown split event')
            group=manifest['story_groups'][uid]
            if group in groups and groups[group]!=split:raise ValueError('story leakage across splits')
            groups[group]=split
    if len(set(e for ids in manifest['splits'].values() for e in ids))!=sum(map(len,manifest['splits'].values())):raise ValueError('overlapping splits')
    dataset=EvaluationDataset(manifest,profiles,events,data['queries'],data['judgments'],content_hash(manifest),data.get('payment_contexts',{}))
    report=validate_judgments(dataset,dataset.judgments)
    if not report['valid']:raise ValueError(report['errors'])
    return dataset

def validate_judgments(dataset, judgments):
    errors=[];seen=set()
    validator=Draft202012Validator(json.loads(Path(__file__).with_name('judgments.schema.json').read_text()))
    errors.extend(e.message for e in validator.iter_errors(judgments))
    for j in judgments:
        try:
            key=(j['company_id'],j['role'],j['event_id'])
            if key in seen:raise ValueError('duplicate judgment')
            seen.add(key);profile=dataset.profiles[j['company_id']];b=dataset.events[j['event_id']]
            if j['company_context_hash']!=payload_hash(profile):raise ValueError('profile hash mismatch')
            if (j['event_version'],j['payload_hash'])!=(b['event']['event_version'],payload_hash(b)):raise ValueError('event hash mismatch')
            if j['story_group_id']!=dataset.manifest['story_groups'][j['event_id']]:raise ValueError('story group mismatch')
            if j['role']!=dataset.manifest['role']:raise ValueError('role mismatch')
            if j['label_source'] not in {'human','weak','synthetic_test'}:raise ValueError('invalid label source')
            if j['status'] not in {'judged','unknown','not_judged'}:raise ValueError('invalid label status')
            if j['status']=='judged':
                if type(j['label']) is not int or j['label'] not in range(4) or not j['annotator']:raise ValueError('invalid judged row')
            elif j['label'] is not None:raise ValueError('unknown is not zero')
            for ref in j['basis_feature_refs']:
                if ref['company_id']==j['company_id']:
                    if ref['profile_version']!=profile['profile_version'] or ref['feature_id'] not in profile['features']:raise ValueError('foreign feature')
                else:
                    neighborhood=(dataset.payment_contexts.get(j['company_id']) or {}).get('neighborhood',{})
                    allowed=[r for d in ('incoming','outgoing') for n in neighborhood.get(d,[]) for r in n.get('feature_refs',[])]
                    if ref not in allowed:raise ValueError('foreign neighbor without payment evidence')
            for ref in j['basis_claim_refs']:
                if ref['event_id']!=j['event_id'] or ref['event_version']!=j['event_version'] or ref['claim_id'] not in {c['claim_id'] for c in b['claims']}:raise ValueError('foreign claim')
            if j['label_source']=='human' and j.get('synthetic'):raise ValueError('synthetic labels are not human quality gold')
        except (KeyError,ValueError,TypeError) as exc:errors.append(str(exc))
    return {'valid':not errors,'errors':errors}
