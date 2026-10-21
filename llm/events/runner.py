"""Sequential B3 worker, one provider request per attempt, durable usage first."""
import threading
from .budget import CallLedger, validate_limits
from .extraction import build_request, parse_extraction, profile_from_spec
from .extraction_models import ExtractionResult, RunOutcome
from .models import Issue
from .provider import ProviderFailure
from .storage import StorageError, canonical_json, processing_hash
from .validation import article_sha256


class LeaseHeartbeat:
    """Inject a context-manager factory in tests; no real-time sleeps needed."""
    def __init__(self,store,token,interval=30):
        self.store,self.token,self.interval=store,token,interval
        self._stop=threading.Event()
        self.lost=False

    def pulse(self):
        try:
            self.store.renew_lease(self.token)
        except Exception:
            self.lost=True
            self._stop.set()

    def _run(self):
        while not self._stop.wait(self.interval):
            self.pulse()

    def __enter__(self):
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.thread.start()
        return self

    def __exit__(self,*_):
        self._stop.set()
        self.thread.join()


class ExtractionRunner:
    def __init__(self,store,provider,*,worker_id='b3-worker',heartbeat=LeaseHeartbeat):
        self.store,self.provider,self.worker_id,self.heartbeat=store,provider,worker_id,heartbeat
        self.ledger=CallLedger(store,offline=not provider.is_live)

    def _preflight(self,spec,limits,*,probe=False):
        profile=profile_from_spec(spec)
        if self.provider.is_live != (profile.execution_mode=='live'):
            raise StorageError('execution_mode_mismatch','Offline responses cannot populate a live processing cache')
        # Probes may test unverified parameters, but never bypass monetary gates.
        checked=profile
        if probe:
            checked=profile.model_copy(update={'format_verified':True,'thinking_verified':True})
        validate_limits(checked,limits,live=self.provider.is_live)
        if self.provider.is_live:
            if profile.model != 'deepseek/deepseek-v4.1-flash':
                raise StorageError('model_not_allowed','This B3 adapter enables only the explicitly supplied DeepSeek model ID')
            if getattr(self.provider,'profile',profile)!=profile:
                raise StorageError('provider_profile_mismatch','Provider settings differ from the processing spec')
            self.provider.check_credentials()
        return profile

    def _terminal(self,job,token,code,*,defer=False,stop=False,call_id=None):
        try:
            if defer:
                self.store.defer_job(job.job_id,code,token=token)
            else:
                self.store.fail_attempt(token,[Issue(code=code,path='/',message=code)])
        except StorageError as exc:
            if exc.code in {'stale_attempt','article_not_current'}:
                return RunOutcome(status='superseded',job_id=job.job_id,call_id=call_id,reason=code,stop=stop)
            raise
        return RunOutcome(status='deferred' if defer else 'failed',job_id=job.job_id,call_id=call_id,reason=code,stop=stop)

    def run_next(self,spec,limits,*,_probe=False,source_id=None):
        profile=self._preflight(spec,limits,probe=_probe)
        job=self.store.claim_next(spec,self.worker_id,source_id=source_id)
        if job is None:
            return RunOutcome(status='idle')
        token=job.active_attempt.token
        record=self.store.get_article(job.source_id)
        request=build_request(record,profile)
        if len(canonical_json(request).encode('utf-8'))>profile.max_request_bytes:
            return self._terminal(job,token,'input_too_large',defer=True)
        try:
            call_id=self.ledger.reserve(token,request,profile,limits)
        except StorageError as exc:
            if exc.code.startswith('budget_'):
                return self._terminal(job,token,exc.code,defer=True,stop=True)
            if exc.code=='stale_attempt':
                return RunOutcome(status='superseded',job_id=job.job_id,reason=exc.code)
            raise
        try:
            self.store.save_artifact(token,'llm_request',request,schema_version='b3.1')
            with self.heartbeat(self.store,token) as heartbeat:
                # Recheck fencing immediately before dispatch. A process killed
                # after reservation leaves a full conservative reservation.
                self.store.renew_lease(token)
                try:
                    response=self.provider.complete(request)
                except ProviderFailure as exc:
                    self.ledger.finish(call_id,error={'code':exc.code,'message':str(exc)})
                    return self._terminal(job,token,exc.code,stop=True,call_id=call_id)
                except Exception:
                    self.ledger.finish(call_id,error={'code':'provider_internal_error','message':'Unexpected provider failure; cost is uncertain'})
                    return self._terminal(job,token,'provider_internal_error',stop=True,call_id=call_id)
                call=self.ledger.finish(call_id,response=response.payload())
            if heartbeat.lost:
                return self._terminal(job,token,'lease_lost',stop=True,call_id=call_id)
            self.store.save_artifact(token,'llm_response',response.payload(),schema_version='b3.1')
            # Record all billed responses, including invalid extraction and HTTP errors.
            blocked=self.ledger.budget()['blocked']
            if response.status_code!=200:
                if response.status_code==429:
                    return self._terminal(job,token,'provider_rate_limited',defer=True,stop=True,call_id=call_id)
                code='provider_auth_error' if response.status_code in (401,403) else 'provider_http_error'
                return self._terminal(job,token,code,stop=True,call_id=call_id)
            body=response.body
            choices=body.get('choices')
            if not isinstance(choices,list) or len(choices)!=1 or not isinstance(choices[0],dict):
                return self._terminal(job,token,'invalid_provider_response',stop=blocked,call_id=call_id)
            choice=choices[0]
            msg=choice.get('message')
            if not isinstance(msg,dict):
                return self._terminal(job,token,'invalid_provider_response',stop=blocked,call_id=call_id)
            if choice.get('finish_reason')=='length':
                return self._terminal(job,token,'output_limit_exceeded',stop=blocked,call_id=call_id)
            if msg.get('refusal') or choice.get('finish_reason')=='content_filter':
                return self._terminal(job,token,'provider_refusal',stop=blocked,call_id=call_id)
            if msg.get('tool_calls') or msg.get('function_call') or choice.get('finish_reason') in {'tool_calls','function_call'}:
                return self._terminal(job,token,'unexpected_tools',stop=blocked,call_id=call_id)
            if choice.get('finish_reason')!='stop':
                return self._terminal(job,token,'unexpected_finish_reason',stop=blocked,call_id=call_id)
            try:
                extracted=parse_extraction(msg.get('content'))
            except StorageError as exc:
                self.store.save_artifact(token,'extraction_errors',[i.model_dump() for i in exc.issues])
                return self._terminal(job,token,exc.code,stop=blocked,call_id=call_id)
            result=ExtractionResult(source_id=record.source_id,article_sha256=article_sha256(record.article),
                processing_spec_hash=processing_hash(spec),extraction=extracted)
            self.store.complete_attempt(token,result.model_dump(mode='json'),kind='extraction_candidates',schema_version='b3.1')
            return RunOutcome(status='succeeded',job_id=job.job_id,call_id=call_id,
                reason='budget_overrun' if blocked else 'cost_unknown' if call['state']=='unknown' else None,stop=blocked)
        except StorageError as exc:
            if exc.code=='stale_attempt':
                return RunOutcome(status='superseded',job_id=job.job_id,call_id=call_id,reason='stale_attempt')
            raise

    def run_pending(self,spec,limits,max_jobs):
        if type(max_jobs) is not int or max_jobs<=0:
            raise StorageError('invalid_argument','max_jobs must be a positive integer')
        results=[]
        for _ in range(max_jobs):
            result=self.run_next(spec,limits)
            results.append(result)
            if result.status=='idle' or result.stop:
                break
        return results
