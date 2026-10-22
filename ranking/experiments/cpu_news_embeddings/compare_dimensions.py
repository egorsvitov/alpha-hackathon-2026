"""Run identical benchmark in isolated processes; shared, unchanged news corpus."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from encoder import download

HERE=Path(__file__).resolve().parent
MODELS={
    384:('small','614241f622f53c4eeff9890bdc4f31cfecc418b3'),
    768:('base','d128750597153bb5987e10b1c3493a34e5a4502a'),
    1024:('large','3d7cfbdacd47fdda877c5cd8a79fbcc4f2a574f3'),
}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--dimensions', type=int, nargs='+', default=[384,768,1024], choices=list(MODELS))
    parser.add_argument('--repeats',type=int,default=3)
    args=parser.parse_args()
    for dim in args.dimensions:
        size,revision=MODELS[dim]
        directory=HERE/'artifacts'/('model' if dim==384 else f'model-{dim}')
        print(f'Preparing {size}: {dim} dimensions',flush=True)
        download(f'intfloat/multilingual-e5-{size}',revision,directory)
        output=HERE/'artifacts'/f'comparison-{dim}'
        subprocess.run([sys.executable,str(HERE/'benchmark.py'),'--cpus','2','--repeats',str(args.repeats),
                        '--model-dir',str(directory),'--output-dir',str(output),'--dimension',str(dim)],check=True)
    reports=[json.loads((HERE/'artifacts'/f'comparison-{d}'/'benchmark.json').read_text()) for d in args.dimensions]
    assert len({r['input_sha256'] for r in reports})==1
    (HERE/'artifacts'/'dimensions-comparison.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__': main()
