"""Idempotent orchestration; paid paths require explicit construction by the CLI."""
from collections import Counter
import time

from retriever.models import RetrievalRequest, digest, instant
from retriever.selection import RetrieverService
from retriever.store import SQLiteSearchStore
from .mapping import map_cards
from .storage import PipelineError, now

VERSION = 'full-cycle.v1'


def run_job(workspace, profiles, company_id, role, as_of, *, analyst, encoder=None, merge=None,
            k=20, raw_fallback=False, include_graph=False, max_merge_calls=0, analysis_policy=None,
            retry_failed=False):
    if role not in ('ceo','cfo'):
        raise PipelineError('unsupported_full_cycle_role')
    instant(as_of)
    policy = analysis_policy or {'max_input_tokens':8000, 'max_projection_chars':24000, 'max_insights':k}
    began = time.monotonic()
    usage_before = analyst.stats.snapshot()
    with workspace.writer():
        job = dict(id=digest([company_id,role,as_of,now()]),company_id=company_id,role=role,as_of=as_of,
                   created_at=now(),status='running',outcomes=[], analysis_policy=policy)
        try:
            profile, interests, _ = profiles.load(company_id,include_graph=include_graph)
            index = workspace.state('index')
            if not index:
                raise PipelineError('index_not_ready')
            if (interests.encoder_fingerprint, interests.dimension) != (index['encoder_fingerprint'],index['dimension']):
                raise PipelineError('interest_index_profile_mismatch')
            signature = dict(version=VERSION,model=analyst.settings.model,prompt=analyst.settings.prompt_version,
                             fewshot=analyst.settings.fewshot,mode=analyst.settings.mode,
                             memory=analyst.settings.memory_enabled, llm_type=type(analyst.llm).__name__)
            context = dict(company=profile,interests=interests.model_dump(mode='json'),role=role,as_of=as_of,
                corpus=index['identity'],k=k,raw_fallback=raw_fallback,include_graph=include_graph,
                merge_config=merge.configuration_fingerprint if merge else None,max_merge_calls=max_merge_calls,
                policy=policy,analyst=signature)
            job_id = digest(context)
            existing = workspace.job(job_id)
            if existing and existing['status'] != 'running' and not retry_failed:
                workspace.set_state('latest:'+company_id+':'+role, {'id':job_id})
                return dict(existing,reused=True)
            if existing and existing['status'] == 'running' and not retry_failed:
                # A previous process stopped after reserving/sending. Never resend
                # automatically; operator must reconcile unknown provider outcomes.
                raise PipelineError('interrupted_job_requires_review')
            if retry_failed and existing and existing.get('status') == 'ready':
                return dict(existing,reused=True)
            job.update(id=job_id,profile_version=interests.profile_version,interest_set_version=interests.interest_set_version,
                       profile_snapshot=profile,interest_snapshot=interests.model_dump(mode='json'),
                       analyst_signature=signature,index_identity=index['identity'])
            workspace.publish_job(job)
            request = RetrievalRequest(request_id=job_id,company_id=company_id,profile_version=interests.profile_version,
                corpus_id=index['corpus_id'],as_of=as_of,k=k,raw_fallback=raw_fallback,include_graph=include_graph,
                max_new_llm_calls=max_merge_calls)
            with SQLiteSearchStore(index['path']) as store:
                result = RetrieverService().retrieve(request,interests=interests,store=store,merge=merge,encoder=encoder)
            batch = result.batch.model_dump(mode='json')
            job.update(batch=batch,retrieval_trace=result.trace)
            workspace.publish_job(job)  # Preserve expensive retrieval before analysis.
            output, adapted = analyst.analyze_batch(batch,role,company_profile=profile,
                active_interests=interests.model_dump(mode='json'),analysis_policy=policy)
            job.update(analyst_output=output.model_dump(mode='json'),adapter_diagnostics=adapted.diagnostics(),
                       gateway_usage_before=usage_before,gateway_usage_after=analyst.stats.snapshot())
            outcomes = [dict(candidate_id=i.candidate_id,canonical_event_id=next(
                c.canonical_event_id for c in adapted.analyst_input.candidates if c.candidate_id==i.candidate_id),
                status='insufficient_evidence' if i.support.inference_status=='insufficient_evidence' else i.decision)
                for i in output.ranked_insights]
            for excluded in output.excluded_candidates:
                code = excluded.failure_code or excluded.reason_code
                status = ('budget_exhausted' if code == 'budget_exhausted' else 'analysis_failed') if excluded.reason_code == 'analysis_failed' else ('exclude' if code == 'not_relevant' else code)
                if status not in ('exclude','insufficient_evidence','analysis_failed','budget_exhausted','context_too_large'):
                    status = 'insufficient_evidence'
                outcomes.append(dict(candidate_id=excluded.candidate_id,status=status,reason_code=code))
            known = {o['candidate_id'] for o in outcomes}
            for d in adapted.candidates:
                if d.get('candidate_id') not in known and d.get('state') not in ('ready','duplicate'):
                    outcomes.append(dict(candidate_id=d.get('candidate_id'),status=d.get('state','analysis_failed')))
            for d in batch['deferred']:
                code = 'unresolved' if 'unresolved' in d.get('reason','') else 'deferred'
                outcomes.append(dict(cluster_id=d['cluster_id'],status=code,reason_code=d.get('reason')))
            technical = any(o['status'] not in ('include','watch','exclude') for o in outcomes)
            job.update(outcomes=outcomes,status='partial' if technical or batch['status']=='partial' else 'ready',
                counts=dict(Counter(o['status'] for o in outcomes)),costs=output.run_metadata.model_dump(mode='json',exclude={'candidates','input_diagnostics'}),
                seconds=time.monotonic()-began,completed_at=now())
            cards = map_cards(job,output,adapted,batch,profile)
            workspace.publish_job(job,cards)
            return dict(job,reused=False)
        except Exception as exc:
            code = exc.code if isinstance(exc,PipelineError) else 'job_failed:'+type(exc).__name__
            job.update(status='failed',error_code=code,gateway_usage_before=usage_before,gateway_usage_after=analyst.stats.snapshot(),seconds=time.monotonic()-began,completed_at=now())
            workspace.publish_job(job)
            return dict(job,reused=False)
