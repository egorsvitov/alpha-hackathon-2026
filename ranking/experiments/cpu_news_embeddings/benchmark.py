"""Measure complete CPU encoding (including tokenization), not collection/LLM."""
import os
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['OMP_NUM_THREADS'] = '2'
os.environ['OPENBLAS_NUM_THREADS'] = '2'
os.environ['MKL_NUM_THREADS'] = '2'

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import gc
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import random
import resource
import sys
import time

import numpy as np
from tokenizers import Tokenizer
from encoder import Encoder, MODEL_DIR, MODEL_ID, REVISION

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from ranking.b6 import load_bundles, require_valid_bundle


def stats(values):
    x = np.asarray(values)
    return {key: float(np.percentile(x, q)) for key,q in [('min',0),('p50',50),('p95',95),('max',100)]}


def company_sample(limit=256):
    # Real local records, only in ignored artifacts; not an expert evaluation set.
    with (ROOT/'data/companies/df_Uid_data_hack.csv').open(encoding='utf-8-sig',newline='') as f:
        records = list(csv.DictReader(f,delimiter=';'))
    multiplicity = Counter(r['SYNTHETIC_U'] for r in records)
    by_code = {}
    for r in records:
        if multiplicity[r['SYNTHETIC_U']] == 1:
            by_code.setdefault(r['MAINOKVED_CODE'],r)
    candidates = sorted(by_code.values(),key=lambda r:r['MAINOKVED_CODE'])
    selected = random.Random(42).sample(candidates,min(limit,len(candidates)))
    products = {'CL_PRODUCT_RKO_MARK':'РКО','CL_PRODUCT_LOA_MARK':'кредит','CL_PRODUCT_LEA_MARK':'лизинг',
                'CL_PRODUCT_DEP_MARK':'депозит','CL_PRODUCT_GAR_MARK':'гарантии','FACTORING_MARK':'факторинг'}
    return [{'company_id':r['SYNTHETIC_U'],'okved':r['MAINOKVED_CODE'],
             'text':f"Деятельность: {r['MAINOKVED_NAME']}. ОКВЭД {r['MAINOKVED_CODE']}. "
                    f"Регион регистрации: {r['REGION']}. "
                    f"Банковские продукты: {', '.join(label for col,label in products.items() if r[col] in ('1','1.0')) or 'не указаны'}. "
                    f"Страны ФТС: {r['FTS_COUNTRY_NAME'] or 'не указаны'}."}
            for r in selected]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--cpus',type=int,default=2)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--model-dir', type=Path, default=MODEL_DIR)
    parser.add_argument('--output-dir', type=Path, default=HERE/'artifacts')
    parser.add_argument('--dimension', type=int, default=384)
    args=parser.parse_args()
    if args.cpus<1 or args.repeats<1: raise ValueError('positive CPU/repeat counts required')
    available=sorted(os.sched_getaffinity(0))
    chosen=available[:args.cpus]
    os.sched_setaffinity(0,chosen)
    # numpy/ORT may have imported helper threads already. Pin those too;
    # subsequently created workers inherit the same affinity.
    for task in Path('/proc/self/task').iterdir():
        try: os.sched_setaffinity(int(task.name),chosen)
        except ProcessLookupError: pass
    source=HERE/'artifacts'
    out=args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    manifest=json.loads((args.model_dir/'manifest.json').read_text())
    started=time.perf_counter()
    articles=[json.loads(line) for line in (source/'articles.jsonl').read_text().splitlines()]
    bundles=load_bundles(source/'events.v0.2.jsonl')
    assert len(articles)==len(bundles)==100
    assert len({a['url'] for a in articles})==100
    assert len({a['text_sha256'] for a in articles})==100
    for a,b in zip(articles,bundles):
        require_valid_bundle(b)
        e=b['evidence'][0]
        assert a['text'][e['start']:e['end']]==e['quote']
    modes={'event': [b['event']['title']+'\n'+b['claims'][0]['text'] for b in bundles],
           'article': [a['title']+'\n'+a['text'] for a in articles]}
    # Separate throughput stress test: same longest REAL article repeatedly
    # encoded without result caching, not another 100 independent news items.
    modes['longest_article_stress']=[max(modes['article'],key=len)]*100
    raw_tokenizer=Tokenizer.from_file(str(args.model_dir/'tokenizer.json'))
    raw_tokenizer.no_truncation();raw_tokenizer.no_padding()
    lengths={mode:[len(x.ids) for x in raw_tokenizer.encode_batch(['query: '+t for t in texts])]
             for mode,texts in modes.items()}
    report={'created_at':datetime.now(timezone.utc).isoformat(),
            'cpu': next(l.split(':',1)[1].strip() for l in Path('/proc/cpuinfo').read_text().splitlines() if l.startswith('model name')),
            'cpu_affinity':chosen, 'cpu_limit':len(chosen),'original_available_logical_cpus':len(available),
            'python':platform.python_version(),
            'versions':{p:version(p) for p in ['numpy','onnxruntime','tokenizers','trafilatura']},
            'model':manifest['model_id'],'revision':manifest['revision'],'dimension':args.dimension,'model_files':manifest['files'],'weight_format':'ONNX dynamic INT8 AVX512 VNNI',
            'provider':'CPUExecutionProvider','repeats':args.repeats,'count':100,
            'collection':json.loads((source/'collection.json').read_text()),
            'input_tokens':{m:{'distribution':stats(v),'over_256':sum(n>256 for n in v),'over_512':sum(n>512 for n in v)} for m,v in lengths.items()},
            'input_sha256':hashlib.sha256((source/'articles.jsonl').read_bytes()).hexdigest(),
            'measurements':[]}
    report['stress_note']='longest_article_stress repeats ONE real article 100 times per trial to measure 512-token load, not retrieval quality.'
    for mode,max_tokens in [('event',256),('article',512),('longest_article_stress',512)]:
        t=time.perf_counter();encoder=Encoder(threads=min(2,args.cpus),max_tokens=max_tokens,model_dir=args.model_dir)
        load_seconds=time.perf_counter()-t
        long_text=max(modes[mode],key=len)
        t=time.perf_counter();encoder.encode([long_text]);warmup_seconds=time.perf_counter()-t
        by_batch={}
        for batch_size in (1,8):
            all_latencies=[];trial_seconds=[];vectors=None
            for repeat in range(args.repeats):
                parts=[];start=time.perf_counter()
                for i in range(0,len(articles),batch_size):
                    tick=time.perf_counter()
                    part=encoder.encode(modes[mode][i:i+batch_size])
                    all_latencies.append((time.perf_counter()-tick)*1000)
                    parts.append(part)
                vectors=np.concatenate(parts)
                trial_seconds.append(time.perf_counter()-start)
                assert vectors.shape==(100,args.dimension) and vectors.dtype==np.float32 and np.isfinite(vectors).all()
                assert np.allclose(np.linalg.norm(vectors,axis=1),1,atol=1e-5)
            elapsed=sum(trial_seconds)
            row={'mode':mode,'max_tokens':max_tokens,'batch_size':batch_size,
                 'model_load_seconds':load_seconds,'warmup_seconds':warmup_seconds,
                 'trial_seconds':trial_seconds,'throughput_news_per_minute':100*args.repeats/elapsed*60,
                 'amortized_ms_per_news':elapsed/(100*args.repeats)*1000,
                 'batch_latency_ms':stats(all_latencies),
                 'truncated_inputs':sum(n>max_tokens for n in lengths[mode])}
            report['measurements'].append(row);by_batch[batch_size]=vectors
            print(json.dumps(row,ensure_ascii=False),flush=True)
        cos=(by_batch[1]*by_batch[8]).sum(axis=1)
        # Dynamic INT8 can drift across padded batches: report the failed gate,
        # do not discard measurements or silently label this as quality passed.
        passed=float(cos.min())>0.95
        report.setdefault('batch_consistency_gate',{})[mode]={'threshold':0.95,'passed':passed}
        if not passed:
            print('WARNING batch-dependent embedding drift',mode,stats(cos),flush=True)
        report.setdefault('batch_consistency',{})[mode]=stats(cos)
        if mode=='article':
            np.save(out/'news.embeddings.npy',by_batch[1])
            # Demonstrate actual company lookup on an explicit SMALL sample;
            # do not extrapolate this timing to 85k indexed companies.
            profiles=company_sample()
            t=time.perf_counter()
            company_vectors=np.concatenate([encoder.encode([p['text'] for p in profiles[i:i+8]],prefix='passage: ')
                                            for i in range(0,len(profiles),8)])
            company_seconds=time.perf_counter()-t
            assert company_vectors.shape==(len(profiles),args.dimension)
            assert np.isfinite(company_vectors).all() and np.allclose(np.linalg.norm(company_vectors,axis=1),1,atol=1e-5)
            np.save(out/'companies.sample.embeddings.npy',company_vectors)
            (out/'companies.sample.json').write_text(json.dumps(profiles,ensure_ascii=False,indent=2)+'\n')
            timings=[]
            for v in by_batch[1]:
                tick=time.perf_counter();scores=company_vectors@v
                indices=np.argpartition(-scores,4)[:5]
                indices=indices[np.argsort(-scores[indices])]
                timings.append((time.perf_counter()-tick)*1000)
            examples=[]
            for a,v in zip(articles[:10],by_batch[1][:10]):
                scores=company_vectors@v;indices=np.argsort(-scores)[:5]
                examples.append({'article_id':a['article_id'],'title':a['title'],'url':a['url'],
                                 'candidates':[{'company_id':profiles[j]['company_id'],'okved':profiles[j]['okved'],
                                                'cosine_similarity':float(scores[j]),'description':profiles[j]['text']}
                                               for j in indices]})
            (out/'search.examples.json').write_text(json.dumps(examples,ensure_ascii=False,indent=2)+'\n')
            report['company_search']={'sample_size':len(profiles),'sampling':'one representative per OKVED, random seed 42, max 256',
                                      'company_embedding_seconds':company_seconds,'search_ms':stats(timings),
                                      'quality_status':'not_expert_evaluated; nearest candidates are not proven business impact'}
        del encoder;gc.collect()
    report['peak_rss_mib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
    report['benchmark_process_seconds']=time.perf_counter()-started
    report['timing_scope']='Encoding includes tokenizer, CPU ONNX inference, mean pooling and normalization. Excludes collection, download, model load, warmup, company preparation, LLM and QA.'
    (out/'benchmark.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('Benchmark saved:',out/'benchmark.json',flush=True)


if __name__=='__main__': main()
