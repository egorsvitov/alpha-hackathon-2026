"""BGE-M3 exact-text adapter; imports compatible vectors from semantic cache."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import numpy as np
from llm.event_pipeline.semantic.encoder import SentenceEncoder
from llm.event_pipeline.semantic.core import digest, normalize
from llm.event_pipeline.company_news.contracts import fingerprint
from .embedding_cache import VectorCache, exact_vector_key

REVISION = '5617a9f61b028005a4858fdac845db406aefb181'
MODEL_ROOT = Path('.cache/semantic-bge-m3/hub/models--BAAI--bge-m3/snapshots') / REVISION


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        while data := f.read(1024**2):
            h.update(data)
    return h.hexdigest()


class BGEEncoder:
    def __init__(self, cache_path, *, batch_size=8, old_run=None):
        os.environ.setdefault('HF_HOME', str(Path('.cache/semantic-bge-m3').resolve()))
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA_required_no_fallback')
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        self.encoder = SentenceEncoder(revision=REVISION, device='cuda')
        self.profile = self.encoder.profile
        self.encoder.model.float().eval()
        pooling = self.encoder.model[1].get_config_dict()
        if pooling.get('pooling_mode') != 'cls':
            raise ValueError('expected_BGE_CLS_pooling')
        self.fingerprint_metadata = dict(profile=self.profile, files={str(p.relative_to(MODEL_ROOT)): file_hash(p) for p in MODEL_ROOT.rglob('*') if p.is_file()},
            pooling=pooling, precision='fp32', text_policy='exact-utf8.v1', prefix='', storage='semantic-python-l2-to-float32.v1',
            tf32=False, dimension=1024, truncation='reject', batch_size=batch_size)
        self.fingerprint = fingerprint(self.fingerprint_metadata)
        self.cache = VectorCache(cache_path, dimension=1024)
        self.batch_size = batch_size
        torch.cuda.synchronize()
        self.metrics = dict(load_seconds=time.perf_counter()-started, inference_seconds=0., inferred=0, hits=0, imported=0)
        self.old = None
        if old_run:
            old_run = Path(old_run)
            if json.loads((old_run/'encoder.json').read_text()) != self.profile:
                raise ValueError('old_encoder_profile_incompatible')
            # Prior run encoded with default FP32, no prefix and this pinned ST pooling.
            # Require model files and source implementation identities in the new manifest.
            self.old = sqlite3.connect((old_run/'vectors.sqlite3').resolve().as_uri()+'?mode=ro', uri=True)

    def token_lengths(self, texts, *, input_kind):
        return [len(self.encoder.model.tokenizer(t, truncation=False)['input_ids']) for t in texts]

    def _encode(self, texts, kind):
        import torch
        unique = dict.fromkeys(texts)
        values, pending = {}, []
        for text in unique:
            key = exact_vector_key(self.fingerprint, text, input_kind=kind)
            v = self.cache.get(key)
            if v is not None:
                self.metrics['hits'] += 1
            elif self.old and kind == 'document':
                row = self.old.execute('SELECT vector FROM vectors WHERE key=?', (digest([self.profile, digest(text)]),)).fetchone()
                if row:
                    v = np.asarray(normalize(json.loads(row[0])), dtype=np.float32)
                    self.cache.put(key, v, metadata={'encoder_fingerprint': self.fingerprint, 'input_kind': kind, 'exact_text_sha256': hashlib.sha256(text.encode()).hexdigest()})
                    self.metrics['imported'] += 1
            if v is None:
                pending.append(text)
            else:
                values[text] = v
        for start in range(0, len(pending), self.batch_size):
            batch = pending[start:start+self.batch_size]
            torch.cuda.synchronize()
            began = time.perf_counter()
            vectors, _ = self.encoder.encode(batch)
            torch.cuda.synchronize()
            self.metrics['inference_seconds'] += time.perf_counter()-began
            self.metrics['inferred'] += len(batch)
            for text, vector in zip(batch, vectors):
                v = np.asarray(normalize(vector), dtype=np.float32)
                self.cache.put(exact_vector_key(self.fingerprint, text, input_kind=kind), v,
                    metadata={'encoder_fingerprint': self.fingerprint, 'input_kind': kind, 'exact_text_sha256': hashlib.sha256(text.encode()).hexdigest()})
                values[text] = v
        self.metrics['peak_vram_allocated_bytes'] = torch.cuda.max_memory_allocated()
        self.metrics['peak_vram_reserved_bytes'] = torch.cuda.max_memory_reserved()
        return np.stack([values[t] for t in texts]) if texts else np.empty((0,1024), np.float32)

    def encode_queries(self, texts):
        return self._encode(texts, 'query')

    def encode_documents(self, texts):
        return self._encode(texts, 'document')
