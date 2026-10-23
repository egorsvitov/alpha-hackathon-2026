"""Private artifact handoff. Never include secrets, model weights or working DBs."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
from .io import load_plan,require,save,sha,read


def package(root,output):
    root,output=Path(root),Path(output)
    if root.resolve() in output.resolve().parents:raise ValueError('archive_must_be_outside_payload')
    manifest=load_plan(root)
    for stage in ('index','retrieve','rerank','select','analyze','report'):require(root,stage)
    calls=read(root/'live-calls.json') if (root/'live-calls.json').exists() else []
    calls=[c for c in calls if c.get('request',{}).get('metadata',{}).get('company_news_experiment')==manifest['experiment_id']]
    save(root/'DELIVERY.json',dict(code_commit=subprocess.check_output(['git','rev-parse','HEAD']).decode().strip(),
        branch=subprocess.check_output(['git','branch','--show-current']).decode().strip(),
        confidentiality='PRIVATE: originals and company profiles; do not publish',
        live_calls=len(calls),excluded=['.env','secrets','model weights','SQLite caches','working databases'],
        missing_inputs=['common control344','shared CPU judgments','new live budget/journals'],
        reproduce='Restore this folder under data/processed/company_news; retrieve/select/analyze/report use frozen inputs. Cold plan/index additionally need the original prepared corpus/company CSV/catalogue and pinned models; see repository README.'))
    allowed={'.json','.jsonl','.npy','.html','.md','.png','.txt','.py','.log'}
    paths=[p for p in sorted(root.rglob('*')) if p.is_file() and p.suffix in allowed and not any(s.startswith('.') or s=='__pycache__' for s in p.relative_to(root).parts)]
    # Additional forensic setup logs are copied as regular entries; never glob the repo/env.
    setup=root.parent/'setup'
    entries={str(p.relative_to(root)):p for p in paths}
    if setup.exists():
        entries.update({'setup/'+p.name:p for p in sorted(setup.iterdir()) if p.is_file() and p.suffix in {'.log','.json','.py'}})
    checks=''.join(sha(p)+'  '+name+'\n' for name,p in sorted(entries.items()))
    (root/'ARTIFACTS.sha256').write_text(checks);entries['ARTIFACTS.sha256']=root/'ARTIFACTS.sha256'
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('wb') as f,gzip.GzipFile(filename='',fileobj=f,mode='wb',mtime=0,compresslevel=6) as gz,tarfile.open(fileobj=gz,mode='w|') as tar:
        for name,p in sorted(entries.items()):
            info=tar.gettarinfo(str(p),arcname='company-news-gpu-20261023/'+name)
            info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0;info.mode=0o600
            with p.open('rb') as stream:tar.addfile(info,stream)
    output.with_suffix(output.suffix+'.sha256').write_text(sha(output)+'  '+output.name+'\n')
    output.chmod(0o600)
    return dict(archive=str(output),sha256=sha(output),bytes=output.stat().st_size,files=len(entries),contains_private_sources=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    print(json.dumps(package(a.root,a.output),indent=2))
