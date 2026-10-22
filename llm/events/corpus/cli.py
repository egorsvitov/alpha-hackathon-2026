"""Local corpus preparation and retrieval previews; never calls Telegram or LLM APIs."""
import argparse
import json
from pathlib import Path
from llm.events import StorageError
from .prepare import prepare_corpus
from .neighbors import write_preview


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    prepare=commands.add_parser('prepare')
    prepare.add_argument('--input',type=Path,default=Path('data/raw_news'))
    prepare.add_argument('--output',type=Path,default=Path('data/processed/events/raw-corpus-v1'))
    neighbors=commands.add_parser('neighbors')
    neighbors.add_argument('--corpus',type=Path,required=True);neighbors.add_argument('--output',type=Path,required=True)
    for name,default in [('top-k',3),('per-source',20),('max-groups',30),('window-days',2)]:
        neighbors.add_argument('--'+name,type=int,default=default)
    neighbors.add_argument('--start');neighbors.add_argument('--end')
    args=parser.parse_args(argv)
    try:
        if args.command=='prepare':
            result=prepare_corpus(args.input,args.output)
            result.pop('audit',None)
        else:
            result=write_preview(args.corpus,args.output,top_k=args.top_k,per_source=args.per_source,
                                 max_groups=args.max_groups,window_days=args.window_days,start=args.start,end=args.end)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except StorageError as exc:
        print(json.dumps({'errors':[i.model_dump() for i in exc.issues]},ensure_ascii=False));return 1
    except (OSError,ValueError,TypeError,KeyError):
        print(json.dumps({'errors':[{'code':'invalid_corpus_input','path':'/','message':'Unable to prepare source files or output'}]}));return 1


if __name__=='__main__':raise SystemExit(main())
