"""rank/demo CLI share the trusted store ingestion path."""
import json
import sys
import tempfile
from datetime import datetime,timezone
from pathlib import Path
from .contracts import parse_timestamp,require_valid_contract,FixtureClock
from .engine import load_rules,rank_events

from .b6 import decode_json

def load_json(path):
    return decode_json(path.read_text())

def _write(value,path):
    data=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n'
    if path:
        target=Path(path);target.parent.mkdir(parents=True,exist_ok=True);target.write_text(data)
    else: sys.stdout.write(data)

def _run(args):
    from .b6 import load_bundles
    from .event_store import EventStore,ingest_bundles
    if args.mode=='live' and (args.events or args.fixture_clock): raise ValueError('live rank forbids fixture imports/clocks')
    if args.events and (args.mode!='fixture' or not args.fixture_clock): raise ValueError('--events requires --mode fixture --fixture-clock')
    if args.mode=='fixture' and not args.as_of: raise ValueError('fixture rank requires --as-of')
    if args.fixture_clock and not args.events: raise ValueError('fixture clock requires events import')
    if bool(args.relevance_event_id) != bool(args.relevance_output): raise ValueError('relevance event ID and output must be provided together')
    as_of=parse_timestamp(args.as_of) if args.as_of else datetime.now(timezone.utc)
    profiles=load_json(Path(args.profiles));require_valid_contract(profiles,'company-profiles')
    profile=next((p for p in profiles['profiles'] if p['company_id']==args.company_id),None)
    if profile is None: raise ValueError('company_not_found')
    rules=load_rules(args.rules)
    mapping=load_json(Path(args.entity_mapping)) if args.entity_mapping else None
    with tempfile.TemporaryDirectory(prefix='ranking-fixture-') as temp:
        store=EventStore(Path(temp if args.events else args.state_dir or 'data/processed/personalization')/('fixture-events.sqlite3' if args.mode=='fixture' else 'events.sqlite3'))
        if args.events: ingest_bundles(load_bundles(Path(args.events)),store=store,clock=FixtureClock(args.fixture_clock),source_mode='fixture')
        output=rank_events(store.list_latest(as_of=as_of),profile,role=args.role,as_of=as_of,rules=rules,entity_mapping=mapping,top_k=args.top_k)
        require_valid_contract(output,'ranking-result')
        _write(output,args.output)
        if args.relevance_output:
            row=next((x for x in output['ranked'] if x['snapshot']['event_id']==args.relevance_event_id),None)
            _write(row['relevance'] if row else {'status':'scored_pair_unavailable','event_id':args.relevance_event_id},args.relevance_output)
    store.close()
    return 0

def _demo(args):
    root=Path(__file__).resolve().parents[1];out=Path(args.output_dir)
    out.mkdir(parents=True,exist_ok=True)
    for company in ('DEMO_LOGISTICS','DEMO_IT','DEMO_TRADE'):
        from argparse import Namespace
        _run(Namespace(mode='fixture',events=str(root/'data/examples/personalization/events.jsonl'),fixture_clock='2026-10-20T17:00:00+03:00',as_of=args.as_of,profiles=str(root/'data/examples/personalization/profiles.json'),company_id=company,rules=None,entity_mapping=None,state_dir=None,role='cfo',top_k=5,output=str(out/(company+'.json')),relevance_output=None,relevance_event_id=None))
    _write({'status':'ok','generation_mode':'fixture','output_dir':str(out)},None)
    return 0

def register_parser(subparsers):
    p=subparsers.add_parser('rank');source=p.add_mutually_exclusive_group();source.add_argument('--events');source.add_argument('--state-dir')
    p.add_argument('--profiles',required=True);p.add_argument('--company-id',required=True)
    p.add_argument('--role',choices=['ceo','cfo','treasurer','chief_accountant'],default='ceo');p.add_argument('--mode',choices=['fixture','live'],default='live')
    p.add_argument('--as-of');p.add_argument('--fixture-clock');p.add_argument('--rules');p.add_argument('--entity-mapping');p.add_argument('--top-k',type=int,default=5);p.add_argument('--output');p.add_argument('--relevance-event-id');p.add_argument('--relevance-output');p.set_defaults(func=_run)
    p=subparsers.add_parser('demo');p.add_argument('--as-of',required=True);p.add_argument('--output-dir',required=True);p.set_defaults(func=_demo)
