"""Explicit offline agent commands; no credentials are read."""
import argparse
import json
from pathlib import Path
import sys
from ranking.contracts import PROJECT_ROOT, FixtureClock, parse_timestamp, require_valid_contract
from ranking.b6 import load_bundles, decode_json
from ranking.event_store import EventStore, ingest_bundles
from llm.rag.retriever import retrieve_evidence, load_config
from llm.rag.corpus import Corpus
from .ports import Services
from .run_store import RunStore
from .generators.fixture import FixtureGenerator
from .orchestrator import analyze_event, answer_question

def main(argv=None):
    parser=argparse.ArgumentParser(prog='python -m llm.agent')
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('analyze','ask'):
        p=commands.add_parser(name)
        p.add_argument('--company-id',required=True)
        p.add_argument('--role',required=True,choices=['ceo','cfo','treasurer','chief_accountant'])
        p.add_argument('--mode',choices=['fixture','live'],required=True)
        p.add_argument('--state-dir',required=True)
        p.add_argument('--corpus',help='Explicit corpus; fixture mode defaults to synthetic corpus')
        p.add_argument('--output')
        if name=='analyze':
            p.add_argument('--relevance',required=True,help='Explicit legacy Relevance fixture; no automatic ranking');p.add_argument('--event',required=True);p.add_argument('--profiles',required=True);p.add_argument('--as-of',required=True)
        else:
            p.add_argument('--run-id',required=True);p.add_argument('--question',required=True)
    args=parser.parse_args(argv)
    try:
        if args.mode!='fixture':
            raise ValueError('provider_unavailable: live requires the 06A gateway')
        state=Path(args.state_dir)
        if not state.is_absolute():state=PROJECT_ROOT/state
        state.mkdir(parents=True,exist_ok=True)
        rag=load_config()
        corpus=Corpus(Path(args.corpus) if args.corpus else PROJECT_ROOT/'llm/rag/tests/fixtures/corpus.jsonl',config=rag)
        store=RunStore(state/'runs.sqlite')
        event_store=EventStore(state/'fixture-events.sqlite3')
        services=Services(event_store=event_store,retriever=retrieve_evidence,generator=FixtureGenerator(),run_store=store,corpus=corpus,
                          config={'mode':'fixture','rag':rag})
        if args.command=='analyze':
            at=parse_timestamp(args.as_of);services.clock=FixtureClock(at)
            profiles=decode_json(Path(args.profiles).read_text());require_valid_contract(profiles,'company-profiles')
            profile=next((p for p in profiles['profiles'] if p['company_id']==args.company_id),None)
            if profile is None:raise ValueError('company_not_found')
            bundles=load_bundles(Path(args.event))
            if len(bundles)!=1:raise ValueError('analyze requires exactly one event')
            with EventStore(state/'fixture-events.sqlite3') as events:
                ingest_bundles(bundles,store=events,clock=services.clock,source_mode='fixture')
                snapshot=events.get_latest(bundles[0]['event']['event_id'],as_of=at)
                relevance=decode_json(Path(args.relevance).read_text())
                require_valid_contract(relevance,'relevance')
                result=analyze_event(snapshot,profile,relevance,role=args.role,as_of=at,services=services)
        else:
            result=answer_question(args.run_id,args.question,company_id=args.company_id,role=args.role,services=services)
        data=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
        if args.output:
            Path(args.output).parent.mkdir(parents=True,exist_ok=True);Path(args.output).write_text(data)
        else:sys.stdout.write(data)
        store.close()
        event_store.close()
        return 0
    except (ValueError,OSError,KeyError) as exc:
        print(json.dumps({'error':'agent_error','message':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1

if __name__=='__main__':raise SystemExit(main())
