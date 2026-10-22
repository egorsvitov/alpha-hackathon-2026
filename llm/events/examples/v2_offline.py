"""Reproducible scripted A/B demonstration. No credentials or network."""
import argparse
import json
from pathlib import Path
from llm.events.extraction.service import ExtractionService
from llm.events.extraction.cli import run_experiment
from llm.events.extraction.evaluation import export_comparison
from llm.events.extraction.prompts import DISCOVERY, EXTRACTION
from llm.events.extraction.models import ProviderProfile, ExecutionLimits
from llm.events.extraction.provider import ProviderResponse
from llm.events.storage.store import EventStore

class ScriptedProvider:
    is_live=False
    def __init__(self,fixture):self.fixture=fixture;self.requests=[]
    def complete(self,request):
        self.requests.append(request);prompt=request['messages'][0]['content']
        if prompt.startswith(DISCOVERY):data=self.fixture['discovery']
        elif prompt.startswith(EXTRACTION):data=self.fixture['templates']
        else:
            payload=json.loads(request['messages'][1]['content'].split('\n',1)[1])
            data={'verdicts':[{'candidate_id':c['candidate_id'],'fields':[{
                'path':p,'status':'supported','evidence':[{'fragment_id':'P1','start_id':'P1.S1','end_id':'P1.S1'}]
            } for p in c['required_paths']]} for c in payload['review']]}
        return ProviderResponse({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data,ensure_ascii=False)}}],
            'usage':{'prompt_tokens':100,'completion_tokens':100}})


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=Path('data/processed/events/experiment-v2-offline'))
    args=p.parse_args(argv);args.output.mkdir(parents=True,exist_ok=True)
    fixture=json.loads(Path(__file__).resolve().parents[1].joinpath('fixtures/two-stage.v2.json').read_text())
    article_path=args.output/'articles.json';article_path.write_text(json.dumps([fixture['article']],ensure_ascii=False,indent=2))
    store=EventStore(args.output/'state.sqlite3');provider=ScriptedProvider(fixture)
    profile=ProviderProfile(execution_mode='offline',model='offline:scripted',limits_confirmed=True,
        max_input_tokens=30000,max_output_tokens=4096,context_window_tokens=34096)
    limits=ExecutionLimits(tariffs_confirmed=True,input_rub_per_million='0',output_rub_per_million='0',daily_quota_rub='1')
    service=ExtractionService(store,provider,profile,limits=limits)
    report,calls,bundles=run_experiment(service,article_path,max_jobs=10,experiment_id='synthetic-demo')
    report.update(execution_mode='offline-scripted',real_api_requests=0,
        simulation_notice='All provider replies, verification verdicts and token usage are scripted. No quality claim.')
    export_comparison(args.output/'report',report,calls=calls,bundles=bundles,inputs=[fixture['article']])
    print(json.dumps({'output':str(args.output/'report'),'real_api_requests':0,
        'new_scripted_calls':len(provider.requests),'events_A':len(bundles['A']),'events_B':len(bundles['B'])},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
