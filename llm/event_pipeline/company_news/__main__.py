"""python -m llm.event_pipeline.company_news --help"""
import argparse
import json
from .io import read


def main():
    p=argparse.ArgumentParser(description='Frozen CPU/GPU raw-news retrieval experiment')
    p.add_argument('--root',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    q=sub.add_parser('plan');q.add_argument('--corpus',required=True);q.add_argument('--companies',required=True);q.add_argument('--catalogue',required=True);q.add_argument('--as-of',default='2026-10-23T00:00:00+00:00')
    q=sub.add_parser('index');q.add_argument('--batch-size',type=int,default=8)
    sub.add_parser('retrieve')
    q=sub.add_parser('review-pool');q.add_argument('--split',choices=['dev','test'],required=True);q.add_argument('--depth',type=int,default=1)
    q=sub.add_parser('rerank');q.add_argument('--base-variant',choices=['G0','G1','G2'],required=True);q.add_argument('--batch-size',type=int,default=8)
    q=sub.add_parser('select');q.add_argument('--reserve',type=int,choices=[0,4,8],default=0)
    q=sub.add_parser('analyze');q.add_argument('--model-profile')
    q=sub.add_parser('analyze-live');q.add_argument('--journal',required=True);q.add_argument('--shared-store',required=True);q.add_argument('--profile-path',required=True);q.add_argument('--budget-path',required=True);q.add_argument('--max-calls',type=int,required=True);q.add_argument('--variant',default='G4');q.add_argument('--corpus',default='data/processed/events/news-corpus-v1')
    q=sub.add_parser('evaluate');q.add_argument('--split',choices=['dev','test'],required=True);q.add_argument('--labels-path')
    sub.add_parser('report')
    a=vars(p.parse_args());command=a.pop('command');root=a.pop('root')
    if command in ('plan','index','retrieve'):
        from . import experiment
        result=getattr(experiment,command)(root,**a)
    elif command=='review-pool':
        from .evaluation import make_review_pool
        result=make_review_pool(root,**a)
    elif command=='rerank':
        from .reranking import rerank
        result=rerank(root,**a)
    elif command=='select':
        from .selection import select
        result=select(root,**a)
    elif command=='analyze':
        from .analysis import analyze
        if a['model_profile']:a['model_profile']=read(a['model_profile'])
        result=analyze(root,**a)
    elif command=='analyze-live':
        from .analysis import live
        result=live(root,**a)
    elif command=='evaluate':
        from .evaluation import evaluate
        result=evaluate(root,**a)
        result={k:v for k,v in result.items() if k not in ('per_company','misses')}
    else:
        from .report import report
        result=report(root)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
