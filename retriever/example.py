"""Synthetic plumbing demo. Fake summaries and vectors measure no LLM quality."""
import argparse
import json
from pathlib import Path

import numpy as np

from llm.cluster_merge.service import MergeService

from .importers import corpus_sha256
from .merge_adapter import ClusterMergeAdapter
from .models import (ArticleRevision, ClusterSnapshot, CompanyInterestSet, Interest,
                     RetrievalRequest, SearchItem, VectorRecord, canonical, text_hash)
from .selection import RetrieverService
from .store import SQLiteSearchStore


class SyntheticProvider:
    """Deterministically joins supplied fragments; never performs HTTP."""
    def __init__(self):
        self.calls = 0

    def complete(self, messages, profile):
        self.calls += 1
        fragments = json.loads(messages[1]['content'])['fragments']
        answer = dict(events=[dict(title='Synthetic factory update', paragraphs=[dict(
            text=' '.join(f['text'] for f in fragments), fragment_ids=[f['id'] for f in fragments])])],
            coverage=[dict(fragment_id=f['id'], kind='used') for f in fragments])
        return dict(choices=[dict(finish_reason='stop', message=dict(content=json.dumps(answer)))], usage={})


class SyntheticLimiter:
    def reserve(self, key):
        pass  # Fake provider only; never use this limiter for a network provider.


class SyntheticEncoder:
    fingerprint = 'synthetic-encoder.v1'
    dimension = 2

    def encode_event_texts(self, texts):
        return np.asarray([[1., 0.] for _ in texts], dtype=np.float32)


def encoder_factory():
    return SyntheticEncoder()


def _write(path, value):
    if isinstance(value, list):
        value = [v.model_dump(mode='json') if hasattr(v, 'model_dump') else v for v in value]
    path.write_text(canonical(value)+'\n', encoding='utf-8')


def run_example(directory):
    """Produce native input files and demonstrate cached merge with zero new calls."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    articles = [ArticleRevision(revision_id=key, article_id=key, title='Factory update',
        text=text, source='synthetic-'+key, published_at='2026-10-23', publication_precision='day')
        for key, text in [('a', 'A factory opened a production line.'),
                          ('b', 'The factory announced a new production line.'),
                          ('c', 'An unrelated synthetic weather bulletin.')]]
    items = [SearchItem(item_id=a.revision_id, title=a.title, text=a.text, embedding_text=a.text,
                        source_revision_ids=[a.revision_id]) for a in articles]
    vectors = [VectorRecord(vector_id=a.revision_id, item_id=a.revision_id,
        embedding_text_sha256=text_hash(a.text), values=[1., 0.] if a.revision_id != 'c' else [0., 1.]) for a in articles]
    cluster = ClusterSnapshot(cluster_id='factory', revision='1', member_revision_ids=['a', 'b'])
    interests = CompanyInterestSet(company_id='synthetic-company', profile_version='1', interest_set_version='1',
        encoder_fingerprint=SyntheticEncoder.fingerprint, dimension=2,
        interests=[Interest(interest_id='production', text='Production facilities', vector=[1., 0.])])
    request = RetrievalRequest(request_id='synthetic-request', company_id=interests.company_id, profile_version='1',
        corpus_id='synthetic-corpus.v1', as_of='2026-10-24T00:00:00Z', k=2)
    bundle = dict(schema_version='retriever-vector-bundle.v1', corpus_id=request.corpus_id,
        corpus_sha256=corpus_sha256(articles), article_sources={a.revision_id: a.text_sha256 for a in articles},
        encoder_fingerprint=SyntheticEncoder.fingerprint, dimension=2,
        items=[i.model_dump() for i in items], vectors=[v.model_dump() for v in vectors])
    _write(directory/'articles.json', articles)
    _write(directory/'manifest.json', bundle)
    _write(directory/'clusters.json', [cluster])
    _write(directory/'interests.json', interests)
    _write(directory/'request.json', request)
    provider = SyntheticProvider()
    adapter = ClusterMergeAdapter(MergeService(provider, SyntheticLimiter(), directory/'merge.sqlite3'))
    # Warm cache with a deterministic fake response, then import and retrieve offline.
    prepared = adapter.resolve(cluster, articles[:2], 'a', max_calls=1)
    if prepared.status != 'completed':
        raise RuntimeError('synthetic_cache_preparation_failed')
    with SQLiteSearchStore(directory/'index.sqlite3') as store:
        store.import_bundle(articles=articles, items=items, vectors=vectors, clusters=[cluster],
            encoder_fingerprint=SyntheticEncoder.fingerprint, dimension=2, corpus_id=request.corpus_id)
        run = RetrieverService().retrieve(request, interests=interests, store=store, merge=adapter, encoder=SyntheticEncoder())
    _write(directory/'batch.json', run.batch)
    _write(directory/'trace.json', run.trace)
    return dict(status=run.batch.status, returned=len(run.batch.candidates), new_calls=run.trace['calls_new'],
                fake_provider_calls=provider.calls, quality_claim='Synthetic integration check only')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='Local synthetic artifact directory')
    print(json.dumps(run_example(parser.parse_args().output), sort_keys=True))


if __name__ == '__main__':
    main()
