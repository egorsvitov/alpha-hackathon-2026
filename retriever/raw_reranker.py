"""Pinned multilingual cross encoder. No truncation, retries or OOM fallback."""
import importlib.metadata
import json
from pathlib import Path
import sqlite3
import time
from .raw_encoder import file_hash
from llm.event_pipeline.company_news.contracts import fingerprint

REVISION='953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e'
MODEL='BAAI/bge-reranker-v2-m3'
ROOT=Path('.cache/company-news-models/BAAI--bge-reranker-v2-m3')


class CrossEncoder:
    def __init__(self, cache, *, batch_size=8, max_pair_tokens=2048):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        if not torch.cuda.is_available():raise RuntimeError('CUDA_required')
        root=ROOT/REVISION;verified=json.loads((ROOT/'verified.json').read_text())
        if verified['revision'] != REVISION or verified['model'] != MODEL:raise ValueError('unverified_model')
        for name,digest in verified['files'].items():
            if file_hash(root/name)!=digest:raise ValueError('reranker_artifact_changed:'+name)
        torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
        torch.cuda.reset_peak_memory_stats();start=time.perf_counter()
        self.tokenizer=AutoTokenizer.from_pretrained(root,local_files_only=True,trust_remote_code=False)
        self.model=AutoModelForSequenceClassification.from_pretrained(root,local_files_only=True,trust_remote_code=False,
            use_safetensors=True,attn_implementation='sdpa').float().to('cuda').eval()
        if not 1<=max_pair_tokens<=self.model.config.max_position_embeddings-2:raise ValueError('pair_token_limit')
        self.max_pair_tokens=max_pair_tokens;self.batch_size=batch_size
        self.profile=dict(model=MODEL,revision=REVISION,files=verified['files'],precision='fp32',device=torch.cuda.get_device_name(0),
            max_pair_tokens=max_pair_tokens,truncation='unscored-preserve-retrieval',tf32=False,batch_size=batch_size,
            attention='sdpa',torch=torch.__version__,transformers=importlib.metadata.version('transformers'),
            score='raw-logit-not-probability',aggregation='max-logit-per-family-then-family-RRF.v1')
        self.fingerprint=fingerprint(self.profile)
        self.cache=sqlite3.connect(cache);self.cache.execute('CREATE TABLE IF NOT EXISTS pairs(key TEXT PRIMARY KEY,result TEXT NOT NULL)');self.cache.commit()
        torch.cuda.synchronize();self.metrics=dict(load_seconds=time.perf_counter()-start,inference_seconds=0.,inferred_pairs=0,cache_hits=0,unscored=0,batches=0)

    def score_pairs(self, pairs):
        import torch
        keys=[fingerprint([self.fingerprint,q,d]) for q,d in pairs];values={};pending={}
        for key,pair in zip(keys,pairs):
            row=self.cache.execute('SELECT result FROM pairs WHERE key=?',(key,)).fetchone()
            if row:values[key]=json.loads(row[0]);self.metrics['cache_hits']+=1
            else:pending[key]=pair
        items=list(pending.items())
        for off in range(0,len(items),self.batch_size):
            batch=items[off:off+self.batch_size];texts=[v for _,v in batch]
            encoded=self.tokenizer(texts,truncation=False,padding=False)
            good=[]
            for i,ids in enumerate(encoded['input_ids']):
                if len(ids)>self.max_pair_tokens:
                    values[batch[i][0]]=dict(status='unscored',reason='pair_token_limit',tokens=len(ids),score=None);self.metrics['unscored']+=1
                else:good.append(i)
            if good:
                torch.cuda.synchronize();start=time.perf_counter()
                try:
                    tensors=self.tokenizer([texts[i] for i in good],truncation=False,padding=True,return_tensors='pt').to('cuda')
                    with torch.inference_mode():scores=self.model(**tensors).logits.flatten().float().cpu().tolist()
                    torch.cuda.synchronize()
                except torch.cuda.OutOfMemoryError:
                    (Path(str(self.cache.execute('PRAGMA database_list').fetchone()[2])).parent/'reranker-oom.json').write_text(json.dumps(dict(batch=off,pair_lengths=[len(x) for x in encoded['input_ids']],allocated=torch.cuda.memory_allocated(),reserved=torch.cuda.memory_reserved(),action='stopped_explicit_resume_required')))
                    raise
                self.metrics['inference_seconds']+=time.perf_counter()-start;self.metrics['batches']+=1;self.metrics['inferred_pairs']+=len(good)
                for i,score in zip(good,scores):values[batch[i][0]]=dict(status='scored',score=score,tokens=len(encoded['input_ids'][i]))
            with self.cache:
                for key,_ in batch:self.cache.execute('INSERT INTO pairs VALUES(?,?)',(key,json.dumps(values[key])))
        self.metrics.update(peak_vram_allocated_bytes=torch.cuda.max_memory_allocated(),peak_vram_reserved_bytes=torch.cuda.max_memory_reserved())
        return [values[key] for key in keys]

    def rerank(self, company_query, documents):
        return self.score_pairs([(company_query,d) for d in documents])
