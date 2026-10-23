"""Explicit, resumable transport; pinned official LFS identity is authoritative."""
import concurrent.futures
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import time
import urllib.request

REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
MODEL = "BAAI/bge-reranker-v2-m3"
SHA = "d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286"
SIZE = 2271071852
CHUNK = 4 * 1024**2
ROOT = Path(".cache/company-news-models/BAAI--bge-reranker-v2-m3")


def main():
    root = ROOT / REVISION
    parts = ROOT / "parts"
    root.mkdir(parents=True, exist_ok=True)
    parts.mkdir(exist_ok=True)
    official = f"https://huggingface.co/{MODEL}/resolve/{REVISION}/"
    # Mirror is a transport only: the official pinned revision's size+SHA must match.
    transport = f"https://modelscope.cn/models/{MODEL}/resolve/master/model.safetensors"
    started = time.monotonic()
    info_path=Path('data/processed/company_news/setup/reranker-model-info.json')
    if not info_path.exists():
        info_path.parent.mkdir(parents=True,exist_ok=True)
        with urllib.request.urlopen(f'https://huggingface.co/api/models/{MODEL}/revision/{REVISION}?blobs=true',timeout=90) as response:
            info_path.write_bytes(response.read())
    info = json.loads(info_path.read_text())
    if info['sha'] != REVISION:
        raise ValueError('metadata_revision_mismatch')
    metadata = {r['rfilename']: r for r in info['siblings']}
    for name in ("config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "sentencepiece.bpe.model", "README.md"):
        if not (root / name).exists():
            lfs = metadata[name].get('lfs', {})
            reused = False
            for p in Path('.cache/semantic-bge-m3/hub').glob('models--BAAI--bge-m3/snapshots/*/' + name):
                if lfs.get('sha256') and hashlib.sha256(p.read_bytes()).hexdigest() == lfs['sha256']:
                    shutil.copyfile(p, root / name)
                    reused = True
                    break
            if not reused:
                with urllib.request.urlopen(official + name + '?download=true', timeout=90) as r:
                    data = r.read()
                (root / name).with_suffix('.tmp').write_bytes(data)
                (root / name).with_suffix('.tmp').replace(root / name)
        data = (root / name).read_bytes()
        entry = metadata[name]
        if entry.get('lfs'):
            valid = len(data) == entry['size'] and hashlib.sha256(data).hexdigest() == entry['lfs']['sha256']
        else:
            valid = len(data) == entry['size'] and hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest() == entry['blobId']
        if not valid:
            raise ValueError('pinned_small_artifact_mismatch:' + name)

    def fetch(index):
        start, end = index * CHUNK, min(SIZE, (index + 1) * CHUNK) - 1
        path = parts / f"{index:04}.part"
        if path.exists() and path.stat().st_size == end - start + 1:
            return {"part": index, "status": "cached"}
        try:
            req = urllib.request.Request(transport + f"?download=true&part={index}", headers={"Range": f"bytes={start}-{end}"})
            with urllib.request.urlopen(req, timeout=35) as r:
                if r.status != 206 or r.headers.get("Content-Range") != f"bytes {start}-{end}/{SIZE}":
                    raise ValueError("range_not_honoured")
                data = r.read()
            if len(data) != end - start + 1:
                raise ValueError("part_length_mismatch")
            path.with_suffix(".tmp").write_bytes(data)
            path.with_suffix(".tmp").replace(path)
            return {"part": index, "status": "downloaded"}
        except Exception as e:
            return {"part": index, "status": "failed", "error_type": type(e).__name__}

    audit = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        for item in pool.map(fetch, range((SIZE + CHUNK - 1) // CHUNK)):
            audit.append(item)
            if len(audit) % 50 == 0:
                print(json.dumps({"parts": len(audit), "failed": sum(x["status"] == "failed" for x in audit)}), flush=True)
    (ROOT / f"transfer-{int(time.time())}.json").write_text(json.dumps(audit, indent=2))
    if any(x["status"] == "failed" for x in audit):
        raise RuntimeError("Missing parts; inspect audit and explicitly resume, no automatic retries")
    h = hashlib.sha256()
    with (root / "weights.tmp").open("wb") as out:
        for i in range(len(audit)):
            data = (parts / f"{i:04}.part").read_bytes()
            h.update(data)
            out.write(data)
    if h.hexdigest() != SHA or (root / "weights.tmp").stat().st_size != SIZE:
        raise ValueError("pinned_lfs_identity_mismatch")
    (root / "weights.tmp").replace(root / "model.safetensors")
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file()}
    # Small artifacts are pinned HF downloads; weights additionally checked against pinned API metadata.
    (ROOT / "verified.json").write_text(json.dumps(dict(model=MODEL, revision=REVISION, official=official, transport=transport, files=files, size=SIZE, seconds=time.monotonic()-started), indent=2))
    print(json.dumps({"verified": True, "seconds": time.monotonic()-started}))


def e5():
    from retriever.embeddings import MODEL_SHA,TOKENIZER_SHA
    root=Path('ranking/experiments/cpu_news_embeddings/artifacts/model');root.mkdir(parents=True,exist_ok=True)
    revision='614241f622f53c4eeff9890bdc4f31cfecc418b3';files={}
    for name,size,digest in [('model_qint8_avx512_vnni.onnx',118346824,MODEL_SHA),('tokenizer.json',17082730,TOKENIZER_SHA)]:
        path=root/name
        if not path.exists():
            url=f'https://huggingface.co/intfloat/multilingual-e5-small/resolve/{revision}/onnx/{name}'
            with urllib.request.urlopen(url,timeout=90) as r,path.with_suffix('.tmp').open('wb') as out:
                while data:=r.read(1024**2):out.write(data)
            path.with_suffix('.tmp').replace(path)
        if path.stat().st_size!=size or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise ValueError('E5_pinned_identity_mismatch:'+name)
        files['onnx/'+name]=dict(bytes=size,sha256=digest)
    (root/'manifest.json').write_text(json.dumps(dict(model_id='intfloat/multilingual-e5-small',revision=revision,files=files),indent=2))


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--e5',action='store_true');args=p.parse_args()
    e5() if args.e5 else main()
