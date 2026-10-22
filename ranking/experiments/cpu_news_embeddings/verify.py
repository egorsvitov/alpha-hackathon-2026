"""Check real corpus grounding, native B6 ingestion and saved vectors."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[2]))
from ranking.b6 import load_bundles
from ranking.event_store import EventStore


def main():
    out=HERE/'artifacts'
    articles=[json.loads(line) for line in (out/'articles.jsonl').read_text().splitlines()]
    bundles=load_bundles(out/'events.v0.2.json')
    assert bundles==load_bundles(out/'events.v0.2.jsonl')
    assert len(articles)==len(bundles)==100
    assert len({a['url'] for a in articles})==100
    assert len({a['text_sha256'] for a in articles})==100
    for a,b in zip(articles,bundles):
        assert a['text_sha256']==hashlib.sha256(a['text'].encode()).hexdigest()
        e=b['evidence'][0]
        assert a['text'][e['start']:e['end']]==e['quote']==b['claims'][0]['text']
    with EventStore(':memory:') as store:
        at=datetime.now(timezone.utc)
        initial=store._batch(bundles,received_at=at,source_mode='live')
        repeat=store._batch(bundles,received_at=at,source_mode='live')
        assert all(r.status=='inserted' for r in initial)
        assert all(r.status=='unchanged' for r in repeat)
        assert store.connection.execute('SELECT COUNT(*) FROM events').fetchone()[0]==100
    vectors=np.load(out/'news.embeddings.npy',allow_pickle=False)
    assert vectors.shape==(100,384) and vectors.dtype==np.float32
    assert np.isfinite(vectors).all()
    assert np.allclose(np.linalg.norm(vectors,axis=1),1,atol=1e-5)
    similarity=vectors@vectors.T
    assert np.all(np.diag(similarity)>=similarity.max(axis=1)-1e-5)
    companies=json.loads((out/'companies.sample.json').read_text())
    company_vectors=np.load(out/'companies.sample.embeddings.npy',allow_pickle=False)
    assert company_vectors.shape==(len(companies),384)
    assert np.isfinite(company_vectors).all()
    assert np.allclose(np.linalg.norm(company_vectors,axis=1),1,atol=1e-5)
    benchmark=json.loads((out/'benchmark.json').read_text())
    assert benchmark['provider']=='CPUExecutionProvider'
    assert benchmark['input_sha256']==hashlib.sha256((out/'articles.jsonl').read_bytes()).hexdigest()
    report={'status':'passed','count':100,'company_sample_size':len(companies),
            'checks':['unique_urls_and_texts','JSON_JSONL_equivalence','text_hashes','exact_quote_spans',
                      'strict_B6_batch_ingest','repeat_ingest_unchanged','vector_shape_dtype_finiteness',
                      'unit_norms','self_similarity','company_vectors','benchmark_input_hash','CPU_provider'],
            'input_sha256':benchmark['input_sha256']}
    (out/'validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
