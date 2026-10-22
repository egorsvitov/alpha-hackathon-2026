"""Fixed offline workflow. Every retrieval/generation is bounded and recorded."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import uuid
from jsonschema import Draft202012Validator, FormatChecker
from ranking.contracts import parse_timestamp
from .context import build_context, ContextError
from .ports import GenerationRequest
from .source_registry import build_source_registry

ROOT = Path(__file__).resolve().parent
PROMPT_VERSION = 'agent-interpret-v1'

def draft_schema():
    return json.loads((ROOT / 'prompts/draft.schema.json').read_text())

def generate_draft(context, services, *, purpose='interpret', attempt=0):
    if services.config.get('mode') != 'fixture' or getattr(services.generator, 'mode', None) != 'fixture':
        raise ValueError('provider_unavailable: live gateway not implemented before 06A')
    request = GenerationRequest(purpose, context.run_id, f'generation:{attempt}', 'offline-fixture-v1',
        PROMPT_VERSION, deepcopy(context.payload), draft_schema(),
        messages=({'role':'system','content':(ROOT/'prompts/system.txt').read_text()},
                  {'role':'user','content':(ROOT/'prompts/user.txt').read_text()}))
    result = services.generator.generate(request)
    if result.mode != 'fixture' or result.model_id != 'offline-fixture-v1' or result.provider_request_id is not None or result.usage:
        raise ValueError('provider_metadata_mismatch')
    return result

def _validate_evidence(evidence, snapshot, profile, as_of, services):
    from ranking.contracts import require_valid_contract
    from llm.rag.retriever import temporal_rejection
    require_valid_contract(evidence, 'evidence-bundle')
    for key, expected in {'event_id': snapshot.event_id, 'event_version': snapshot.event_version,
                          'company_id': profile['company_id'], 'profile_version': profile['profile_version']}.items():
        if evidence[key] != expected:
            raise ValueError('evidence_identity_mismatch:' + key)
    if parse_timestamp(evidence['as_of']) != parse_timestamp(as_of):
        raise ValueError('evidence_identity_mismatch:as_of')
    if services.corpus is not None and evidence['corpus_version'] != services.corpus.version:
        raise ValueError('evidence_corpus_version_mismatch')
    if any(plan['corpus_version'] != evidence['corpus_version'] for plan in evidence['query_plan']):
        raise ValueError('evidence_query_corpus_version_mismatch')
    for hit in evidence['hits']:
        if services.corpus is None:
            raise ValueError('bank_hit_requires_verifiable_corpus')
        services.corpus.verify_hit(hit)
        config = {**services.corpus.config, **services.config.get('rag', {})}
        rejection = temporal_rejection(services.corpus.get_chunk(hit['chunk_id']), parse_timestamp(as_of), config)
        if rejection:
            raise ValueError('evidence_temporal_rejection:' + rejection)

def analyze_event(snapshot, profile, relevance, *, role, as_of, services):
    from .qa import validate_draft
    run_id = str(uuid.uuid4())
    registry = build_source_registry(snapshot)
    context = build_context(snapshot, profile, relevance, run_id=run_id, role=role, as_of=as_of,
                            config=services.config, registry=registry)
    store = services.run_store
    if store is None:
        raise ValueError('run_store_required')
    store.create_run(run_id, context.to_dict())
    artifacts, steps = [], []
    def artifact(kind, value):
        aid=store.save_artifact(run_id, kind, value); artifacts.append(aid); return aid
    def step(state, tool, value, *, inputs=(), started_at=None):
        now=started_at or parse_timestamp(services.clock.now()).isoformat()
        aid=artifact(state, value)
        record={'state':state,'started_at':now,'finished_at':parse_timestamp(services.clock.now()).isoformat(),
                'tool_name':tool,'tool_version':'0.1','input_artifact_ids':list(inputs),
                'output_artifact_ids':[aid],'status':'finished','result':state}
        store.record_step(run_id, record); steps.append(record)
    step('created','create_run', {'run_id':run_id})
    step('inputs_validated','validate_identity',context.snapshot)
    step('context_built','build_context',context.to_dict())
    retrieval_count=0; generation_count=0; report=None; draft=None; evidence=None; error=None
    while True:
        try:
            if evidence is None:
                retriever=services.retriever
                if retriever is None:
                    from llm.rag.retriever import retrieve_evidence
                    retriever=retrieve_evidence
                reason = None if retrieval_count==0 else ('; '.join(report.missing_evidence_queries) or 'qa_missing_evidence')
                attempt=retrieval_count
                retrieval_count+=1
                operation_start=parse_timestamp(services.clock.now()).isoformat()
                evidence=retriever(snapshot.bundle, profile, relevance, as_of=as_of, corpus=services.corpus,
                    config=services.config.get('rag') or services.corpus.config, attempt=attempt, retry_reason=reason)
                artifact('retrieval_result_unvalidated', evidence)
                _validate_evidence(evidence, snapshot, profile, as_of, services)
                registry=build_source_registry(snapshot,evidence)
                context=build_context(snapshot,profile,relevance,run_id=run_id,role=role,as_of=as_of,
                    config=services.config,registry=registry)
                # Excluded bank sources cannot be selected by the generator or finalizer.
                registry={k:v for k,v in registry.items() if k not in context.excluded_artifacts}
                context=replace(context,registry=deepcopy(registry))
                step('evidence_retrieved','retrieve_evidence',evidence,started_at=operation_start)
                artifact('context',context.to_dict());artifact('registry',registry)
            if generation_count>=2:
                error='generation_limit';break
            operation_start=parse_timestamp(services.clock.now()).isoformat()
            result=generate_draft(context,services,purpose='interpret' if generation_count==0 else 'repair',attempt=generation_count)
            generation_count+=1;draft=result.payload
            step('draft_generated','fixture_generator',draft,started_at=operation_start)
            operation_start=parse_timestamp(services.clock.now()).isoformat()
            checker=services.qa or validate_draft
            report=checker.check(draft,context,registry) if hasattr(checker,'check') else checker(draft,context,registry)
            step('qa_checked','validate_draft',report.to_dict(),started_at=operation_start)
            if report.next_action=='deliver' and report.publication_status!='block' and report.structural_status=='passed':
                break
            if report.next_action=='retrieve_more' and retrieval_count<3 and generation_count<2:
                evidence=None;continue
            if report.next_action=='repair_draft' and generation_count<2:
                continue
            error='qa_blocked';break
        except (ValueError, OSError, TypeError, KeyError) as exc:
            error=str(exc)
            step('operation_failed','workflow_error',{'code':type(exc).__name__,'message':error})
            break
    prior_exists=store.has_prior_run(company_id=profile['company_id'],event_id=snapshot.event_id,role=role)
    status='limited'
    if error:
        status='provider_unavailable' if 'provider_unavailable' in error else 'insufficient_evidence'
        draft=None
    # No fixture can become ready without independent semantic review.
    selected_claims=deepcopy(draft['claims']) if draft else []
    source_ids={s for c in selected_claims for s in c['source_ids']}
    feature_ids={f for c in selected_claims for f in c['feature_ids']}
    from .generators.fixture import SECTIONS
    final={'schema_version':'0.2','run_id':run_id,'company_id':profile['company_id'],'profile_version':profile['profile_version'],
        'user_role':role,'as_of':parse_timestamp(as_of).isoformat(),'event_id':snapshot.event_id,'event_version':snapshot.event_version,
        'generation_mode':'fixture','generated_at':parse_timestamp(services.clock.now()).isoformat(),'status':status,
        'relevance':deepcopy(relevance),'changes_since_previous':{'status':'unknown' if prior_exists else 'new','previous_insight_id':None,'summary':None,'claim_ids':[]},
        'claims':selected_claims,'sources':[deepcopy(registry[s]) for s in sorted(source_ids)],
        'features_used':[deepcopy(profile['features'][f]) for f in sorted(feature_ids)],
        'section_states':deepcopy(draft['section_states']) if draft else {s:{'status':'insufficient_evidence','reason':error} for s in SECTIONS},
        'unknowns':deepcopy(draft['unknowns']) if draft else [],
        'limitations':list(dict.fromkeys([*profile['limitations'],*relevance['limitations'],*((evidence or {}).get('limitations',[])),
              *(draft['limitations'] if draft else []),*([error] if error else [])])),
        'trace':{'artifact_ids':artifacts,'qa_status':report.status if report else 'not_run',
            'retry_count':max(0,retrieval_count-1)+max(0,generation_count-1),'retrieval_count':retrieval_count,'generation_count':generation_count,
            'semantic_review_count':0,'steps':steps,'corpus_version':(evidence or {}).get('corpus_version'),
            'context_fingerprint':context.fingerprint,'prompt_version':PROMPT_VERSION,'model_id':'offline-fixture-v1',
            'config':deepcopy(services.config),'qa':report.to_dict() if report else None},
        'usage':{'input_tokens':0,'output_tokens':0,'cost_rub':0,'pricing_status':'no_llm_calls'}}
    step('stopped' if error else 'completed','finalize',{'status':status})
    final['trace']['artifact_ids']=list(artifacts);final['trace']['steps']=list(steps)
    schema=json.loads((ROOT/'contracts/insight.v0.2.schema.json').read_text())
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(final)
    store.finish_run(run_id,final)
    return final

def answer_question(run_id, question, *, company_id, role, services):
    from .questions import answer_question as answer
    return answer(run_id,question,company_id=company_id,role=role,services=services)
