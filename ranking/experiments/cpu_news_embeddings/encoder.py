"""Pinned multilingual E5 INT8 ONNX encoder, CPU only; no PyTorch/CUDA."""
import hashlib
import json
from pathlib import Path
import time
import urllib.request

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

MODEL_ID = 'intfloat/multilingual-e5-small'
REVISION = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
FILES = ['onnx/model_qint8_avx512_vnni.onnx', 'onnx/tokenizer.json', 'README.md']
MODEL_DIR = Path(__file__).resolve().parent / 'artifacts/model'


def download(model_id=MODEL_ID, revision=REVISION, model_dir=MODEL_DIR):
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    manifest = {'model_id': model_id, 'revision': revision, 'files': {}}
    for name in FILES:
        path = model_dir / Path(name).name
        if not path.exists():
            url = f'https://huggingface.co/{model_id}/resolve/{revision}/{name}'
            started = time.perf_counter()
            temp = path.with_suffix(path.suffix+'.part')
            with urllib.request.urlopen(url, timeout=90) as response, temp.open('wb') as output:
                while block := response.read(1024*1024):
                    output.write(block)
            temp.replace(path)
            print('Downloaded', name, path.stat().st_size, 'bytes in', round(time.perf_counter()-started,2),'s',flush=True)
        manifest['files'][name] = {'bytes': path.stat().st_size,
                                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    (model_dir/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest


class Encoder:
    def __init__(self, threads=2, max_tokens=512, model_dir=MODEL_DIR):
        model_dir = Path(model_dir)
        self.max_tokens = max_tokens
        self.tokenizer = Tokenizer.from_file(str(model_dir/'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=max_tokens)
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(model_dir/'model_qint8_avx512_vnni.onnx'),
                                            sess_options=options, providers=['CPUExecutionProvider'])
        assert self.session.get_providers() == ['CPUExecutionProvider']
        self.inputs = {x.name for x in self.session.get_inputs()}

    def encode(self, texts, prefix='query: '):
        encodings = self.tokenizer.encode_batch([prefix + t for t in texts])
        mask = np.asarray([e.attention_mask for e in encodings], dtype=np.int64)
        available = {'input_ids': np.asarray([e.ids for e in encodings],dtype=np.int64),
                     'attention_mask': mask,
                     'token_type_ids': np.zeros_like(mask)}
        hidden = self.session.run(None,{k:available[k] for k in self.inputs})[0]
        weights = mask[...,None].astype(np.float32)
        vectors = (hidden.astype(np.float32)*weights).sum(axis=1)/weights.sum(axis=1).clip(min=1)
        vectors /= np.linalg.norm(vectors,axis=1,keepdims=True).clip(min=1e-12)
        return vectors


if __name__ == '__main__':
    download()
