"""Small experiment artifacts with immutable stages and integrity checks."""
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import time
from .contracts import canonical, fingerprint


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        while data := f.read(1024**2):
            h.update(data)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def rows(path):
    return [json.loads(s) for s in Path(path).read_text().splitlines() if s.strip()]


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(canonical(value)+'\n')
    os.replace(tmp, path)


def save_rows(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(''.join(canonical(v)+'\n' for v in values))
    os.replace(tmp, path)


def code_hashes():
    paths = list(Path('llm/event_pipeline/company_news').glob('*.py'))+list(Path('retriever').glob('raw_*.py'))
    return {str(p): sha(p) for p in sorted(paths)}


def load_plan(root):
    root = Path(root)
    m = read(root/'manifest.json')
    if fingerprint(m) != (root/'manifest.sha256').read_text().strip():
        raise ValueError('manifest_changed')
    for name, digest in read(root/'plan-files.json').items():
        if sha(root/name) != digest:
            raise ValueError('plan_input_changed:'+name)
    return m


def complete(root, stage, paths, metrics):
    save(Path(root)/(stage+'.stage.json'), dict(stage=stage, files={str(Path(p).relative_to(root)): sha(p) for p in paths},
        metrics=metrics, code=code_hashes(), peak_process_ram_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024))


def require(root, stage):
    record = read(Path(root)/(stage+'.stage.json'))
    for name, digest in record['files'].items():
        if sha(Path(root)/name) != digest:
            raise ValueError('stale_stage:'+stage+':'+name)
    return record
