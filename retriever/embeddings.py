"""Offline, pinned E5 encoder. Prefixing belongs exclusively to this module."""
from pathlib import Path
import hashlib
import json
import platform
from importlib.metadata import version
import numpy as np
from .representation_contracts import EncoderConfig, EncoderUnavailable, InputTooLong, normalize_embedding_text, content_hash

MODEL_SHA = 'dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88'
TOKENIZER_SHA = '0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39'


def masked_mean(hidden, mask):
    weights = np.asarray(mask, dtype=np.float32)[..., None]
    pooled = (np.asarray(hidden, dtype=np.float32) * weights).sum(axis=1) / weights.sum(axis=1).clip(min=1)
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    if not np.isfinite(pooled).all() or (norms <= 1e-12).any():
        raise ValueError('invalid encoder output')
    return np.asarray(pooled / norms, dtype=np.float32)


class Encoder:
    def __init__(self, config=None, *, model_dir=None):
        self.config = config or EncoderConfig()
        config = self.config
        if config.device_policy == 'cuda' or config.backend != 'onnx' or config.precision != 'int8':
            raise EncoderUnavailable('CPU encoder requires onnx/int8; CUDA needs an approved FP32 backend')
        directory = model_dir or config.model_dir
        if not directory:
            raise EncoderUnavailable('model_dir required; prepare artifacts explicitly')
        directory = Path(directory)
        try:
            manifest = json.loads((directory / 'manifest.json').read_text())
            if manifest['model_id'] != config.model_id or manifest['revision'] != config.revision or config.revision != EncoderConfig().revision:
                raise ValueError('unapproved model/revision')
            for name, approved in [('model_qint8_avx512_vnni.onnx', MODEL_SHA), ('tokenizer.json', TOKENIZER_SHA)]:
                path = directory / name
                entry = manifest['files']['onnx/' + name]
                if path.stat().st_size != entry['bytes'] or entry['sha256'] != approved or hashlib.sha256(path.read_bytes()).hexdigest() != approved:
                    raise ValueError('artifact checksum mismatch: ' + name)
            from tokenizers import Tokenizer
            import onnxruntime as ort
            self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
            self.tokenizer.no_truncation()
            self.tokenizer.no_padding()
            options = ort.SessionOptions()
            options.intra_op_num_threads = config.threads
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            self.session = ort.InferenceSession(str(directory / 'model_qint8_avx512_vnni.onnx'), sess_options=options, providers=['CPUExecutionProvider'])
            if self.session.get_providers() != ['CPUExecutionProvider']:
                raise ValueError('unexpected provider')
            self.inputs = {x.name for x in self.session.get_inputs()}
            if self.session.get_outputs()[0].type != 'tensor(float)':
                raise ValueError('unexpected hidden state dtype')
        except Exception as exc:
            raise EncoderUnavailable(str(exc)) from exc
        semantic = config.to_dict()
        semantic.pop('model_dir')
        semantic.pop('device_policy')
        self.fingerprint_metadata = dict(semantic, provider='CPUExecutionProvider', model_sha256=MODEL_SHA,
            tokenizer_sha256=TOKENIZER_SHA, pooling='masked-mean-l2.v1', normalization='nfc-whitespace.v1',
            prefix_policy='e5-query-passage.v1', dimension=384, output_dtype='float32',
            hardware_execution_class=platform.machine() + ':cpu-int8-avx512-vnni', truncation_policy='reject', onnxruntime=version('onnxruntime'), tokenizers=version('tokenizers'), numpy=version('numpy'))
        self.fingerprint = content_hash(self.fingerprint_metadata)
        self.fallback_reason = 'CUDA backend unavailable; selected CPU before build' if config.device_policy == 'auto' else None

    def _prepare(self, texts, input_kind):
        if input_kind not in {'query', 'document'}:
            raise ValueError('input_kind must be query or document')
        prefix = 'query: ' if input_kind == 'query' else 'passage: '
        normalized = [normalize_embedding_text(t) for t in texts]
        if any(not t or t.startswith(('query:', 'passage:')) for t in normalized):
            raise ValueError('nonempty, unprefixed text required')
        return [self.tokenizer.encode(prefix + t) for t in normalized]

    def token_lengths(self, texts, *, input_kind):
        return [len(e.ids) for e in self._prepare(texts, input_kind)]

    def _encode(self, texts, kind):
        encoded = self._prepare(texts, kind)
        if any(len(e.ids) > self.config.max_tokens for e in encoded):
            raise InputTooLong('prefixed input exceeds 512 tokens')
        result = []
        for start in range(0, len(encoded), self.config.batch_size):
            batch = encoded[start:start + self.config.batch_size]
            width = max(len(e.ids) for e in batch)
            ids = np.full((len(batch), width), 1, dtype=np.int64)
            mask = np.zeros_like(ids)
            for row, e in enumerate(batch):
                ids[row, :len(e.ids)] = e.ids
                mask[row, :len(e.ids)] = 1
            available = dict(input_ids=ids, attention_mask=mask, token_type_ids=np.zeros_like(mask))
            result.append(masked_mean(self.session.run(None, {k: available[k] for k in self.inputs})[0], mask))
        output = np.concatenate(result) if result else np.empty((0, 384), dtype=np.float32)
        if output.shape != (len(encoded), 384):
            raise ValueError('unexpected embedding dimension')
        return output

    def encode_queries(self, texts):
        return self._encode(texts, 'query')

    def encode_documents(self, texts):
        return self._encode(texts, 'document')


EmbeddingEncoder = Encoder
