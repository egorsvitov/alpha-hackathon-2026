"""Warm retrieval request latency: frozen query vectors + BM25 + cosine + fusion."""
from collections import defaultdict
from pathlib import Path
import resource
import time
import numpy as np
from retriever.raw_search import RawLexicalIndex,exact_topk,family_fusion
from .experiment import embedding_text,eligible
from .io import load_plan,require,rows,read,save


def benchmark(root):
    root=Path(root);m=load_plan(root);require(root,'index')
    docs=rows(root/'documents.jsonl');allowed={d['reference']['document_id'] for d in docs if eligible(d,m['as_of']) is None}
    lexical=RawLexicalIndex({d['reference']['document_id']:embedding_text(d) for d in docs if d['reference']['document_id'] in allowed})
    start=time.perf_counter();lexical.search('Синтетический прогрев индекса');build_seconds=time.perf_counter()-start
    companies=defaultdict(list)
    for q in rows(root/'performance-queries.jsonl'):companies[q['company_id']].append(q)
    result=dict(lexical_build_seconds=build_seconds,scope='warm retrieval with cached query vectors; includes per-request BM25, exact CPU cosine, RRF and caps; excludes encoders, reranker and LLM',variants={})
    for variant,name,families in [('C2','e5',{'facts'}),('G0','bge',{'facts'}),('G2','bge',{'facts','mechanisms','hypotheses'})]:
        meta=read(root/(name+'-index.json'));matrix=np.load(root/(name+'-documents.npy'),mmap_mode='r');qmatrix=np.load(root/(name+'-queries.npy'),mmap_mode='r')
        qmap={q:i for i,q in enumerate(meta['query_texts'])};positions=[i for i,uid in enumerate(meta['document_ids']) if uid in allowed]
        def blocks():
            for off in range(0,len(positions),512):
                ix=positions[off:off+512];yield [meta['document_ids'][i] for i in ix],matrix[ix]
        timings=[]
        for uid,queries in sorted(companies.items()):
            selected=[q for q in queries if q['family'] in families and q['text'] in qmap];hits=[];start=time.perf_counter()
            scores=exact_topk(qmatrix[[qmap[q['text']] for q in selected]],blocks())
            for q,dense in zip(selected,scores):
                for rank,(did,score) in enumerate(dense,1):hits.append(dict(document_id=did,query_id=q['query_id'],family=q['family'],channel='dense',rank=rank,raw_score=score))
                for h in lexical.search(q['text']):hits.append(dict(document_id=h['document_id'],query_id=q['query_id'],family=q['family'],channel='lexical',rank=h['rank'],raw_score=h['raw_score']))
            family_fusion(hits);timings.append(time.perf_counter()-start)
        result['variants'][variant]=dict(companies=len(timings),p50_seconds=float(np.median(timings)),p95_seconds=float(np.percentile(timings,95)),companies_per_second=len(timings)/sum(timings),total_seconds=sum(timings))
    result['peak_process_ram_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
    save(root/'retrieval-throughput.json',result);return result


if __name__=='__main__':
    import argparse,json
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);a=p.parse_args();print(json.dumps(benchmark(a.root),indent=2))
