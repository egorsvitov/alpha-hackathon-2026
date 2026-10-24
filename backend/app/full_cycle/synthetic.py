"""Explicit synthetic acceptance fixture. Never used by persisted API as fallback."""
from collections import defaultdict
import json
from pathlib import Path

import numpy as np

from app.analyst.llm_gateway import FakeLLM
from app.analyst.service import AnalystService
from app.analyst.settings import AnalystSettings
from llm.cluster_merge import MergeService, SQLiteQuota
from retriever.merge_adapter import ClusterMergeAdapter
from retriever.models import digest
from .profiles import ProfileStore
from .ingest import ingest, atomic_json
from .jobs import run_job
from .storage import now

AS_OF = '2026-10-24T12:00:00+03:00'


class SyntheticEncoder:
    fingerprint = 'synthetic-full-cycle.v1'
    dimension = 2

    def __init__(self):
        self.calls = 0

    def encode_event_texts(self,texts):
        self.calls += len(texts)
        return np.array([[1.,0.] if 'MERGED' in t else [0.,1.] for t in texts],dtype=np.float32)


class SyntheticMergeProvider:
    def __init__(self):
        self.calls = 0

    def complete(self,messages,profile):
        self.calls += 1
        fragments = json.loads(messages[1]['content'])['fragments']
        groups = defaultdict(list)
        for f in fragments:
            marker = next((m for m in ('MERGED','SPLIT_B','SPLIT_C') if m in f['text']),None)
            if marker is None:
                raise ValueError('synthetic_marker_required')
            groups[marker].append(f)
        answer = dict(events=[dict(title='СИНТЕТИКА '+key,paragraphs=[dict(
            text=' '.join(f['text'] for f in fs if f['field']=='text'),fragment_ids=[f['id'] for f in fs])]) for key,fs in sorted(groups.items())],
            coverage=[dict(fragment_id=f['id'],kind='used') for f in fragments])
        return dict(choices=[dict(finish_reason='stop',message=dict(content=json.dumps(answer,ensure_ascii=False)))],
                    usage=dict(prompt_tokens=100,completion_tokens=100))


def analyst_draft():
    return dict(decision='include',event_status='occurred',impact_direction='negative',impact_channels=['costs'],
        materiality='medium',urgency='medium',headline='Синтетическое событие требует проверки',
        what_happened=[dict(text='Источник сообщает о событии.',kind='fact',claim_ids=['C1'])],
        company_relevance=dict(text='Событие касается отрасли компании.',kind='inference',feature_ids=['F1'],claim_ids=['C1']),
        impact_mechanism=dict(text='Влияние может пройти через издержки.',kind='inference',feature_ids=['F1'],claim_ids=['C1']),
        risks=[],opportunities=[],next_steps=[dict(text='Проверить влияние на издержки.',roles=['cfo'],feature_ids=['F1'])],
        recommended_checks=[],unknowns=[],impact_estimate=None,bank_assessment=dict(status='not_found',bank_chunk_ids=[]),
        inference_status='supported')


def setup(workspace):
    corpus = workspace.root/'collector'/'news_corpus.jsonl'
    corpus.parent.mkdir(parents=True,exist_ok=True)
    if not corpus.exists():
        rows = []
        for name,marker in [('a','MERGED'),('b','MERGED'),('c','SPLIT_B'),('d','SPLIT_C')]:
            rows.append(dict(article_id='synthetic:'+name,source='SYNTHETIC Publisher '+name,
                url='https://example.org/synthetic/'+name,title='СИНТЕТИКА '+marker,
                text='СИНТЕТИКА '+marker+': опубликовано изменение условий производства.',
                published_at=None if name=='d' else '2026-10-23T12:00:00+03:00',language='ru'))
        corpus.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    companies = workspace.root/'companies'
    entries = []
    for company_id in ('SYNTHETIC_FACTORY','SYNTHETIC_LOGISTICS'):
        fid = company_id+':/okved/name'
        profile = dict(company_id=company_id,profile_version='synthetic-profile-1',is_synthetic=True,
            features={fid:dict(feature_id=fid,path='/okved/name',value='Синтетическое производство',quality='observed',
                origin=dict(kind='synthetic',source_path='public-fixture'))})
        interests = dict(company_id=company_id,profile_version=profile['profile_version'],interest_set_version='synthetic-interests-1',
            encoder_fingerprint=SyntheticEncoder.fingerprint,dimension=2,interests=[
                dict(interest_id='costs',text='Возможное изменение издержек, гипотеза по отрасли',family='activity',active=True,vector=[1.,0.]),
                dict(interest_id='logistics',text='Условия производства и перевозки, зависимость требует проверки',family='activity',active=True,vector=[0.,1.])])
        display = dict(company_id=company_id,name='Синтетическая '+company_id,profile_version=profile['profile_version'],is_synthetic=True,
            industry='Синтетическое производство',revenue_bn_rub=None,age_label='Неизвестно',region='Неизвестно',products={},fts={})
        atomic_json(companies/(company_id+'-profile.json'),profile)
        atomic_json(companies/(company_id+'-interests.json'),interests)
        entries.append(dict(company_id=company_id,profile=company_id+'-profile.json',profile_sha256=digest(profile),
            interests=company_id+'-interests.json',interests_sha256=digest(interests),display=display))
    atomic_json(companies/'manifest.json',dict(schema_version='full-cycle-companies.v1',companies=entries))
    return corpus,ProfileStore(companies,workspace)


def services(workspace):
    encoder = SyntheticEncoder()
    provider = SyntheticMergeProvider()
    quota = SQLiteQuota(workspace.root/'synthetic-quota.sqlite3',100)
    merge = ClusterMergeAdapter(MergeService(provider,quota,workspace.root/'synthetic-merge.sqlite3'))
    fake = FakeLLM(lambda request:json.dumps(analyst_draft(),ensure_ascii=False),max_json_repairs=0)
    analyst = AnalystService(AnalystSettings(mode='llm',runtime_cache_dir=workspace.root/'synthetic-analyst-cache',
        bundled_cache_dir=workspace.root/'empty-cache',prompt_version='analyst-v1.2',max_parallel=1),llm=fake,bank_evidence=[])
    return encoder,provider,merge,analyst,fake


def run(workspace):
    corpus,profiles = setup(workspace)
    enc,provider,merge,analyst,fake = services(workspace)
    imported = ingest(workspace,corpus,encoder=enc,grouping_threshold=0.9)
    jobs = [run_job(workspace,profiles,company,role,AS_OF,analyst=analyst,encoder=enc,merge=merge,k=10,max_merge_calls=10)
            for company in ('SYNTHETIC_FACTORY','SYNTHETIC_LOGISTICS') for role in ('ceo','cfo')]
    workspace.set_state('worker',dict(status='ready',heartbeat=now(),synthetic=True))
    return dict(mode='synthetic',ingestion={k:imported[k] for k in ('changed','article_count','group_count','new_articles')},
        jobs=[dict(id=j['id'],status=j['status'],counts=j.get('counts'),error_code=j.get('error_code'),reused=j['reused']) for j in jobs],
        new_fake_merge_calls=provider.calls,new_fake_analyst_calls=len(fake.requests),new_encoded=enc.calls)
