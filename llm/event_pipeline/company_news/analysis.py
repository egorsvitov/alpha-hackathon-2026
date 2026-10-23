"""Lazy analyst plans and opt-in audited network execution with existing journals."""
from collections import Counter
from pathlib import Path
import math
from backend.app.analyst.raw_adapter import analysis_input, prepare, validate_card, validate_personal
from .contracts import RawNewsCandidateBatch, NewsAnalysisInput, fingerprint
from .io import rows, read, save, save_rows, sha, load_plan, require, complete


def analyze(root, *, model_profile=None):
    root=Path(root);load_plan(root);require(root,'select')
    from llm.event_pipeline.runtime import ProviderProfile,request
    model_profile=ProviderProfile.model_validate(model_profile or dict(max_tokens=2048)).model_dump(mode='json')
    profiles={p['company_id']:p for p in rows(root/'profiles.jsonl')};cards={};plans=[];usage={}
    for row in rows(root/'selected.jsonl'):
        b=RawNewsCandidateBatch.model_validate(row['batch']);value=analysis_input(b,profiles[b.company_id]);p=prepare(value,model_profile=model_profile)
        plans.append(dict(company_id=b.company_id,variant=row['variant'],input=value.model_dump(),prepared=p))
        for c in p['common']:
            existing=cards.setdefault(c['key'],dict(c,used_by=[]))
            existing['used_by'].append(dict(company_id=b.company_id,variant=row['variant']))
        v=usage.setdefault(row['variant'],dict(contexts=0,card_references=0,card_keys=set(),personal_keys=set(),estimated_full_request_tokens=0))
        v['contexts']+=1;v['card_references']+=len(p['common']);v['card_keys'].update(c['key'] for c in p['common']);v['personal_keys'].add(p['personal']['key'])
        profile=ProviderProfile.model_validate(model_profile)
        # Includes system, user and request envelope. Estimate is labelled, never called exact.
        for c in p['common']:
            req=request(profile,c['prompt'],c['data']);v['estimated_full_request_tokens']+=math.ceil(len(__import__('json').dumps(req,ensure_ascii=False).encode())/4)
    for v in usage.values():
        v['unique_cards']=len(v.pop('card_keys'));v['unique_personal_analyses']=len(v.pop('personal_keys'));v['potential_cross_company_card_hits']=v['card_references']-v['unique_cards']
        v['live_calls']=0;v['actual_usage_tokens']=None;v['cost_rub']=None;v['counter']='utf8_bytes_div4_estimate_only_common_requests_personal_requires_card_output'
    save_rows(root/'analysis-plans.jsonl',plans);save_rows(root/'common-card-requests.jsonl',cards.values())
    save(root/'analysis-accounting.json',dict(state='prepared_not_sent',variants=usage,total_unique_common_cards=len(cards),
        network_blockers=['existing_shared_quota_journal_max_calls_budget_not_supplied','confirmed_live_profile_and_tariffs_required'],
        deterministic_preview='original_quotes_only_not_LLM_summary',semantic_support='not_assessed'))
    complete(root,'analyze',[root/'analysis-plans.jsonl',root/'common-card-requests.jsonl',root/'analysis-accounting.json'],dict(prepared_contexts=len(plans),unique_cards=len(cards),live_calls=0))
    return dict(prepared_contexts=len(plans),unique_cards=len(cards),live_calls=0)


def live(root, *, journal, shared_store, profile_path, budget_path, max_calls, variant='G4',corpus='data/processed/events/news-corpus-v1'):
    """No quota creation, no gateway retries/repair; failed jobs require explicit retry elsewhere.

    The store is the existing shared monetary journal, journal is the existing
    finite request counter. Both are mandatory so a new output folder resets neither.
    """
    if type(max_calls) is not int or max_calls<1 or not all(Path(p).is_file() for p in (journal,shared_store,profile_path,budget_path)):
        raise ValueError('existing_journals_profile_budget_and_finite_max_calls_required')
    from llm.event_pipeline.runtime import Runner,CreditLedger,EventStore,ProviderProfile,OpenAIProvider,ExecutionLimits,request
    from llm.events.extraction.runner import OneShotRunner
    from llm.events.extraction.budget import validate_limits
    from llm.events.extraction.admin_cli import load_cli_env
    from llm.events.storage.models import ProcessingSpec
    from llm.events.storage.store import source_id_for,canonical_json,StorageError
    from llm.events.extraction.credits import RequestCounter
    root=Path(root);m=load_plan(root);require(root,'analyze');require(root,'select')
    corpus_file=Path(corpus)/'documents.jsonl'
    if sha(corpus_file)!=m['inputs']['corpus']:raise ValueError('live_source_corpus_changed')
    original={d['document_id']:d for d in rows(corpus_file)}
    profile=ProviderProfile.model_validate(read(profile_path));limits=ExecutionLimits.model_validate(read(budget_path));validate_limits(profile,limits,live=True)
    if profile.execution_mode!='live':raise ValueError('live_profile_required')
    load_cli_env(Path('llm/events/.env'));provider=OpenAIProvider(profile);provider.check_credentials()
    store=EventStore(shared_store)
    plans=[r for r in rows(root/'analysis-plans.jsonl') if r['variant']==variant]
    expected=profile.model_dump(mode='json')
    if any(prepare(r['input'],model_profile=expected)!=r['prepared'] for r in plans):raise ValueError('planned_model_differs_rebuild_analysis_plan')
    auth=dict(experiment=m['experiment_id'],journal=str(Path(journal).resolve()),store=str(Path(shared_store).resolve()),max_calls=max_calls,
        profile=profile.model_dump(mode='json'),limits=limits.model_dump(mode='json'),variant=variant)
    authpath=root/'live-authorization.json'
    if authpath.exists() and read(authpath)!=auth:raise ValueError('live_authorization_changed')
    save(authpath,auth)
    # Immutable authorization in the shared store prevents a new directory from raising the cap.
    with store._transaction(write=True) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS company_news_authorizations(experiment TEXT PRIMARY KEY, authorization TEXT NOT NULL)')
        encoded=canonical_json(auth)
        old=conn.execute('SELECT authorization FROM company_news_authorizations WHERE experiment=?',(m['experiment_id'],)).fetchone()
        if old and old[0]!=encoded:raise ValueError('shared_authorization_changed')
        conn.execute('INSERT OR IGNORE INTO company_news_authorizations VALUES (?,?)',(m['experiment_id'],encoded))

    class DualLedger:
        def __init__(self,primary,credit):self.primary=primary;self.credit=credit;self.mapping={}
        def reserve(self,token,req,prof,lim,guard=None):
            # The session's finite cap lives in the shared store, not process memory.
            def finite_guard(conn):
                count=conn.execute("SELECT COUNT(*) FROM llm_calls WHERE json_extract(request_json,'$.metadata.company_news_experiment')=?",(m['experiment_id'],)).fetchone()[0]
                if count>=max_calls:raise StorageError('budget_session_calls_exhausted','Finite experiment call cap reached')
                if guard:guard(conn)
            primary_id=self.primary.reserve(token,req,prof,lim,guard=finite_guard)
            try:credit_id=self.credit.reserve(token,req,prof,lim)
            except Exception:
                self.primary.finish(primary_id,error={'code':'shared_counter_reservation_failed','cost':'reserved_conservatively'});raise
            self.mapping[primary_id]=credit_id
            return primary_id
        def finish(self,cid,response=None,error=None):
            # Both persist response/usage before parsing; crashes retain worst-case reservation.
            self.credit.finish(self.mapping[cid],response,error)
            return self.primary.finish(cid,response=response,error=error)
        def budget(self):return self.primary.budget()

    class RawRunner(Runner):
        def _preflight(self,spec,limits,**kwargs):return OneShotRunner._preflight(self,spec,limits,**kwargs)
        def output_for(self,content,record,spec):
            from llm.event_pipeline.extraction import decode
            result=decode(content);config=spec.configuration
            value=NewsAnalysisInput.model_validate(config['input']);docs={d.reference.document_id:d for d in value.batch.documents}
            try:
                from .contracts import SourceSpan
                allowed=[SourceSpan.model_validate({k:v for k,v in s.items() if k!='quote'}) for source in config.get('card_sources',[]) for s in source['spans']]
                checked=validate_card(result,docs,allowed) if config['kind']=='common_card' else validate_personal(result,config['prepared'])
            except (ValueError,TypeError,KeyError) as exc:raise StorageError('raw_analysis_validation_failed',str(exc)) from None
            return checked,'raw_news_'+config['kind'],'raw-analyst.v1'

    runner=RawRunner(store,provider);runner.ledger=DualLedger(runner.ledger,CreditLedger(store,journal));results=[]
    cache=root/'analyst-cache';cache.mkdir(exist_ok=True)
    for plan in plans:
        value=NewsAnalysisInput.model_validate(plan['input']);b=value.batch
        source=b.documents[0]
        # Reuse prepared articles; no invented news or B6 records.
        article=original[source.reference.document_id]['article'];store.import_batch(canonical_json([article]).encode());sid=source_id_for(article['article_id'],1)
        prepared=plan['prepared'];available={}
        for task in prepared['common']+[prepared['personal']]:
            path=cache/(task['key']+'.json')
            if path.exists():
                cached=read(path)
                if cached['cache_key']!=task['key'] or fingerprint(cached['output'])!=cached['sha256']:raise ValueError('analyst_cache_corrupt')
                available[task['key']]=cached['output'];results.append(dict(key=task['key'],status='cached',calls=0));continue
            if task['kind']=='personal_analysis' and not set(prepared['personal']['data']['card_keys'])<=available.keys():break
            data=dict(task['data'])
            if task['kind']=='personal_analysis':data['cards']={k:available[k] for k in task['data']['card_keys']}
            req=request(profile,task['prompt'],data)
            # Metadata is part of the audited request and durable finite cap; never credentials.
            req['metadata']={'company_news_experiment':m['experiment_id']}
            request_bytes=len(canonical_json(req).encode())
            estimate=math.ceil(request_bytes/4)
            preflights=read(root/'live-request-preflight.json') if (root/'live-request-preflight.json').exists() else []
            preflights.append(dict(key=task['key'],full_request_bytes=request_bytes,input_token_estimate=estimate,
                counter='utf8_bytes_div4_estimate_only_not_exact',verified_profile_max_input_tokens=profile.max_input_tokens))
            save(root/'live-request-preflight.json',preflights)
            if request_bytes>profile.max_request_bytes or estimate>profile.max_input_tokens:
                results.append(dict(key=task['key'],status='unprocessed',reason='full_request_byte_limit'));break
            spec=ProcessingSpec(processor_name='company_news.'+task['kind'],processor_version='1',configuration=dict(profile=profile.model_dump(mode='json'),request=req,input=plan['input'],prepared=prepared,kind=task['kind'],cache_key=task['key'],card_sources=task['data'].get('sources',[])))
            job=store.enqueue(sid,spec)
            outcome=runner.run_next(spec,limits,source_id=sid).model_dump() if job.state=='pending' else dict(status=job.state,reason='cached_or_explicit_retry_required')
            job=store.get_job(job.job_id)
            if job.result_artifact_id:
                output=store.get_artifact(job.result_artifact_id).payload
                save(path,dict(cache_key=task['key'],output=output,sha256=fingerprint(output)));available[task['key']]=output
            results.append(dict(key=task['key'],**outcome));save(root/'live-results.json',results)
            save(root/'live-calls.json',runner.ledger.primary.list_calls())
            if outcome.get('stop') or outcome['status'] not in ('succeeded','cached'):return results
    return results
