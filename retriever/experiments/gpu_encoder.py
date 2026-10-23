"""Pinned E5 FP32 on CUDA, isolated from the production ONNX/INT8 encoder."""
import importlib.metadata
from pathlib import Path
import time

import numpy as np

from retriever.representation_contracts import content_hash, normalize_embedding_text, InputTooLong
from .activity_catalogue import sha256

MODEL_ID = 'intfloat/multilingual-e5-small'
REVISION = '614241f622f53c4eeff9890bdc4f31cfecc418b3'


class GPUEncoder:
    def __init__(self, model_path, batch_size=16):
        import torch
        from transformers import AutoModel, AutoTokenizer
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA unavailable; no silent CPU fallback')
        if batch_size < 1:
            raise ValueError('positive batch size required')
        self.torch, self.batch_size = torch, batch_size
        root = Path(model_path)
        if root.name != REVISION:
            raise ValueError('expected pinned Hugging Face snapshot directory')
        self.tokenizer = AutoTokenizer.from_pretrained(root, local_files_only=True, trust_remote_code=False)
        self.model = AutoModel.from_pretrained(root, local_files_only=True, trust_remote_code=False,
                                              use_safetensors=True, attn_implementation='eager')
        torch.backends.cuda.matmul.allow_tf32 = False
        torch.backends.cudnn.allow_tf32 = False
        self.model = self.model.float().to('cuda').eval()
        files = {p.name: sha256(p) for p in root.iterdir() if p.is_file()}
        self.fingerprint_metadata = dict(model_id=MODEL_ID, revision=REVISION, files=files,
            backend='pytorch-cuda', precision='float32', dimension=384,
            pooling='masked-mean-l2.v1', prefix_policy='e5-query-passage.v1',
            normalization='nfc-whitespace.v1', max_tokens=512, truncation_policy='reject',
            torch=torch.__version__, transformers=importlib.metadata.version('transformers'),
            cuda=torch.version.cuda, device=torch.cuda.get_device_name(0), batch_size=batch_size)
        self.fingerprint = content_hash(self.fingerprint_metadata)
        self.elapsed_seconds = 0.0
        self.encoded_texts = 0

    def _texts(self, texts, input_kind):
        prefix = {'query': 'query: ', 'document': 'passage: '}[input_kind]
        texts = [normalize_embedding_text(t) for t in texts]
        if any(not t or t.startswith(('query:', 'passage:')) for t in texts):
            raise ValueError('nonempty unprefixed input required')
        return [prefix + t for t in texts]

    def token_lengths(self, texts, *, input_kind):
        encoded = self.tokenizer(self._texts(texts, input_kind), truncation=False, padding=False)
        return [len(ids) for ids in encoded['input_ids']]

    def _encode(self, texts, kind):
        torch = self.torch
        if any(n > 512 for n in self.token_lengths(texts, input_kind=kind)):
            raise InputTooLong('GPU input exceeds 512 tokens; no truncation')
        prepared = self._texts(texts, kind)
        result = []
        torch.cuda.synchronize()
        started = time.perf_counter()
        with torch.inference_mode():
            for offset in range(0, len(prepared), self.batch_size):
                batch = self.tokenizer(prepared[offset:offset+self.batch_size], padding=True,
                                       truncation=False, return_tensors='pt').to('cuda')
                hidden = self.model(**batch).last_hidden_state
                mask = batch['attention_mask'].unsqueeze(-1).to(hidden.dtype)
                pooled = (hidden * mask).sum(1) / mask.sum(1)
                vectors = torch.nn.functional.normalize(pooled, p=2, dim=1)
                result.append(vectors.cpu().numpy().astype(np.float32))
        torch.cuda.synchronize()
        self.elapsed_seconds += time.perf_counter() - started
        self.encoded_texts += len(texts)
        matrix = np.concatenate(result) if result else np.empty((0, 384), dtype=np.float32)
        if matrix.shape != (len(texts), 384) or not np.isfinite(matrix).all():
            raise ValueError('invalid GPU vectors')
        if len(matrix) and not np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-5):
            raise ValueError('GPU vectors not normalized')
        return matrix

    def encode_queries(self, texts):
        return self._encode(texts, 'query')

    def encode_documents(self, texts):
        return self._encode(texts, 'document')
