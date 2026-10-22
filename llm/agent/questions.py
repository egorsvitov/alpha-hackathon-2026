"""Follow-up answers use pinned evidence and immutable child runs."""
from copy import deepcopy
from dataclasses import replace
import uuid
from ranking.contracts import parse_timestamp, payload_hash, canonical_json
from .context import RunContext
from .source_registry import build_source_registry


def answer_question(run_id, question, *, company_id, role, services):
    from .orchestrator import generate_draft
    from .qa import validate_draft
    if not isinstance(question,str) or not question.strip() or len(question.encode()) > services.config.get('max_question_bytes',8192):
        raise ValueError('invalid_question')
    store = services.run_store
    saved = store.get_run(run_id)
    pinned = deepcopy(saved['context'])
    # Authorize scope before reading/returning answer artifacts.
    if pinned['profile']['company_id'] != company_id or pinned['role'] != role:
        raise PermissionError('run_scope_mismatch')
    for artifact in saved['artifacts']:
        if artifact['kind'] == 'context':
            pinned = deepcopy(artifact['payload'])
    if pinned['profile']['company_id'] != company_id or pinned['role'] != role:
        raise PermissionError('run_scope_mismatch')
    context = RunContext(**pinned)
    child_id = str(uuid.uuid4())
    historical = services.config.get('historical_replay') is True
    payload = deepcopy(context.payload)
    # A question can contain new facts, but these are unverified assumptions only.
    payload['question'] = question
    payload['user_supplied_assumptions'] = [{'text':question,'provenance':'user_supplied','verified':False}]
    context = replace(context,run_id=child_id,payload=payload,
                      fingerprint=payload_hash({'parent_fingerprint':context.fingerprint,'question':question,'historical_replay':historical,'question_config':services.config}))
    store.create_run(child_id,context.to_dict(),parent_run_id=run_id)
    extra = False
    artifacts = []
    def record(state, value, tool):
        start = parse_timestamp(services.clock.now()).isoformat()
        aid = store.save_artifact(child_id,state,value)
        artifacts.append(aid)
        store.record_step(child_id,{'state':state,'tool_name':tool,'tool_version':'0.1','started_at':start,
                                   'finished_at':parse_timestamp(services.clock.now()).isoformat(),
                                   'input_artifact_ids':[],'output_artifact_ids':[aid],'status':'finished','result':state})
    def stopped(reason):
        result={'run_id':child_id,'parent_run_id':run_id,'status':'insufficient_evidence','answer':None,
                'claims':[],'evidence_ids':[],'feature_ids':[],'unknowns':[], 'limitations':[reason],
                'did_extra_search':extra,'historical_replay':historical,'usage':{'pricing_status':'no_llm_calls'},
                'artifact_ids':list(artifacts)}
        record('stopped',{'reason':reason},'question_guard')
        result['artifact_ids']=list(artifacts)
        store.finish_run(child_id,result)
        return result
    record('created',{'parent_run_id':run_id,'question':question},'answer_question')
    if len(canonical_json(context.payload).encode()) > context.config.get('max_context_bytes',131072):
        return stopped('context_overflow')
    if not historical:
        if services.event_store is None:
            return stopped('current_event_unavailable')
        try:
            current = services.event_store.get_latest(context.snapshot['event_id'],services.clock.now())
        except (KeyError, ValueError):
            return stopped('current_event_unavailable')
        if current.bundle['event']['availability'] == 'withdrawn':
            return stopped('event_withdrawn_recalculation_required')
        if current.event_version != context.snapshot['event_version'] or current.payload_sha256 != context.snapshot.get('payload_sha256'):
            return stopped('event_updated_recalculation_required')
    try:
        if services.config.get('mode') != 'fixture':
            return stopped('provider_unavailable')
        if services.config.get('question_extra_search',True) and not any(s.get('type') == 'bank_analytics' for s in context.registry.values()):
            retriever = services.retriever
            if retriever is None:
                from llm.rag.retriever import retrieve_evidence
                retriever = retrieve_evidence
            if services.corpus is None:
                return stopped('corpus_not_configured')
            extra = True # set only at the actual bounded call boundary
            evidence = retriever(context.snapshot['bundle'],context.profile,context.relevance,
                as_of=parse_timestamp(context.as_of),corpus=services.corpus,config=context.config.get('rag',{}),
                attempt=1,retry_reason='question_missing_bank_evidence')
            record('evidence_retrieved',evidence,'retrieve_evidence')
            from .orchestrator import _validate_evidence
            from types import SimpleNamespace
            _validate_evidence(evidence,SimpleNamespace(**context.snapshot),context.profile,parse_timestamp(context.as_of),services)
            # Reuse the same bounded context builder; no excluded hit is eligible.
            from .context import build_context
            registry = build_source_registry(context.snapshot,evidence)
            bounded = build_context(context.snapshot,context.profile,context.relevance,run_id=child_id,
                role=role,as_of=context.as_of,config=context.config,registry=registry)
            registry = {k:v for k,v in registry.items() if k not in bounded.excluded_artifacts}
            bounded_payload = deepcopy(bounded.payload)
            bounded_payload.update(question=question,user_supplied_assumptions=payload['user_supplied_assumptions'])
            context=replace(bounded,registry=registry,payload=bounded_payload,
                fingerprint=payload_hash({'context':bounded.fingerprint,'question':question,'historical_replay':historical,'question_config':services.config}))
        if len(canonical_json(context.payload).encode()) > context.config.get('max_context_bytes',131072):
            return stopped('context_overflow')
        record('context',context.to_dict(),'question_context')
        result = generate_draft(context,services,purpose='answer_question')
        record('draft_generated',result.payload,'fixture_generator')
        checker=services.qa or validate_draft
        qa=checker.check(result.payload,context,context.registry) if hasattr(checker,'check') else checker(result.payload,context,context.registry)
        record('qa_checked',qa.to_dict(),'validate_draft')
        if qa.publication_status == 'block' or qa.structural_status != 'passed' or qa.next_action != 'deliver':
            return stopped('qa_blocked')
        claims = deepcopy(result.payload['claims'])
        answer={'run_id':child_id,'parent_run_id':run_id,'status':'limited','answer':'\n'.join(c['text'] for c in claims),
                'claims':claims,'evidence_ids':sorted({s for c in claims for s in c['source_ids']}),
                'feature_ids':sorted({f for c in claims for f in c['feature_ids']}),
                'unknowns':deepcopy(result.payload['unknowns']),'limitations':[*result.payload['limitations'],*(['historical_replay'] if historical else [])],
                'did_extra_search':extra,'historical_replay':historical,'generation_mode':result.mode,
                'user_supplied_assumptions':deepcopy(payload['user_supplied_assumptions']),
                'qa':qa.to_dict(),'usage':{'pricing_status':'no_llm_calls'},'artifact_ids':list(artifacts)}
        record('completed',{'status':'limited'},'answer_question')
        answer['artifact_ids']=list(artifacts)
        store.finish_run(child_id,answer)
        return answer
    except (ValueError,OSError,TypeError,KeyError) as exc:
        record('error',{'code':type(exc).__name__,'message':str(exc)},'answer_question')
        return stopped('question_processing_failed')
