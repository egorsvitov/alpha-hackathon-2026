"""Atomic conservative RUB reservations and durable call audit in B2's SQLite."""
from decimal import Decimal, InvalidOperation, localcontext
from datetime import datetime
from zoneinfo import ZoneInfo
from uuid import uuid4
import json
from .storage import StorageError, canonical_json
from .extraction_models import ExecutionLimits, ProviderProfile

MOSCOW = ZoneInfo('Europe/Moscow')


def money(value, name, *, positive=False):
    try:
        if not isinstance(value,str):
            raise ValueError()
        result=Decimal(value)
        if not result.is_finite() or result<0 or (positive and result==0) or len(value)>80 or abs(result.as_tuple().exponent)>30:
            raise ValueError()
        return result
    except (InvalidOperation,ValueError,TypeError):
        raise StorageError('invalid_budget_configuration',f'{name} must be a finite nonnegative decimal string') from None


def validate_limits(profile: ProviderProfile, limits: ExecutionLimits, *, live):
    profile=ProviderProfile.model_validate(profile)
    limits=ExecutionLimits.model_validate(limits)
    if live and not limits.live_enabled:
        raise StorageError('live_calls_disabled','Real API requests are disabled')
    if not limits.tariffs_confirmed or not profile.limits_confirmed:
        raise StorageError('budget_configuration_required','Confirmed RUB tariffs and model bounds are required')
    if not profile.max_input_tokens or not profile.max_output_tokens or not profile.context_window_tokens:
        raise StorageError('model_limits_required','Verified input, output and context bounds are required')
    if profile.max_tokens>profile.max_output_tokens or profile.max_input_tokens+profile.max_tokens>profile.context_window_tokens:
        raise StorageError('incompatible_model_limits','Selected output limit exceeds the verified model bounds')
    if live and profile.response_format!='text' and not profile.format_verified:
        raise StorageError('unverified_response_format','Verify JSON mode via an explicit provider probe first')
    if live and (profile.thinking is not None or profile.reasoning_effort is not None) and not profile.thinking_verified:
        raise StorageError('unverified_thinking_parameter','Verify reasoning settings via an explicit provider probe first')
    inp=money(limits.input_rub_per_million,'input_rub_per_million')
    out=money(limits.output_rub_per_million,'output_rub_per_million')
    quota=money(limits.daily_quota_rub,'daily_quota_rub',positive=True)
    if quota>500:
        raise StorageError('invalid_budget_configuration','NLP daily quota cannot exceed 500 RUB')
    return inp,out,quota


def maximum_cost(profile, limits):
    inp,out,_=validate_limits(profile,limits,live=False)
    with localcontext() as ctx:
        ctx.prec=100
        return (Decimal(profile.max_input_tokens)*inp+Decimal(profile.max_tokens)*out)/Decimal(1000000)


class CallLedger:
    def __init__(self, store, *, offline=False):
        self.store=store
        self.prefix='offline:' if offline else 'live:'

    def _day(self):
        return self.prefix+datetime.fromtimestamp(self.store.clock(),MOSCOW).date().isoformat()

    @staticmethod
    def _accounted(conn,day):
        with localcontext() as ctx:
            ctx.prec=100
            return sum((Decimal(r[0]) for r in conn.execute('SELECT accounted_rub FROM llm_calls WHERE budget_day=?',(day,))),Decimal(0))

    def reserve(self, token, request, profile, limits):
        inp,out,quota=validate_limits(profile,limits,live=False)
        reserved=maximum_cost(profile,limits)
        tariffs={'input_rub_per_million':str(inp),'output_rub_per_million':str(out),
            'max_input_tokens':profile.max_input_tokens,'max_tokens':profile.max_tokens}
        with self.store._transaction(write=True) as conn:
            now=self.store.clock()
            attempt=self.store._active_attempt(conn,token,now)
            if conn.execute('SELECT 1 FROM llm_calls WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone():
                raise StorageError('attempt_already_called','Each attempt allows at most one reserved provider call')
            # An overrun is a persistent circuit breaker, including across dates.
            if conn.execute('SELECT 1 FROM budget_days WHERE blocked=1 AND budget_day LIKE ?', (self.prefix+'%',)).fetchone():
                raise StorageError('budget_blocked','Budget overrun requires explicit reconciliation')
            day=self._day()
            row=conn.execute('SELECT * FROM budget_days WHERE budget_day=?',(day,)).fetchone()
            if row:
                # Quota is immutable within the day; workers cannot silently raise it.
                if Decimal(row['quota_rub'])!=quota:
                    raise StorageError('budget_quota_mismatch','Daily quota differs from the already recorded quota')
            else:
                conn.execute('INSERT INTO budget_days(budget_day,quota_rub) VALUES (?,?)',(day,str(quota)))
            with localcontext() as ctx:
                ctx.prec=100
                # Carry unresolved reservations forward: a crash near midnight must
                # not release unknown money merely because the date changed.
                carry=sum((Decimal(r[0]) for r in conn.execute("SELECT accounted_rub FROM llm_calls WHERE budget_day<>? AND budget_day LIKE ? AND state IN ('reserved','unknown')",(day,self.prefix+'%'))),Decimal(0))
                if self._accounted(conn,day)+carry+reserved>quota:
                    raise StorageError('budget_exhausted','Insufficient daily NLP quota for the worst-case call')
            call_id=str(uuid4())
            conn.execute("""INSERT INTO llm_calls(call_id,attempt_id,budget_day,created_at,request_json,tariffs_json,reserved_rub,accounted_rub,state)
                VALUES (?,?,?,?,?,?,?,?,'reserved')""",(call_id,attempt['attempt_id'],day,now,
                canonical_json(request),canonical_json(tariffs),str(reserved),str(reserved)))
        return call_id

    def finish(self, call_id, *, response=None, error=None):
        """Record a response even after lease revocation; never publishes a result."""
        response_json=canonical_json(response) if response is not None else None
        error_json=canonical_json(error) if error is not None else None
        with self.store._transaction(write=True) as conn:
            row=self.store._require(conn.execute('SELECT * FROM llm_calls WHERE call_id=?',(call_id,)).fetchone(),'call',call_id)
            if row['finished_at'] is not None or row['state'] not in {'reserved','reconciled'}:
                raise StorageError('call_already_finished','Call outcome is immutable')
            tariff=json.loads(row['tariffs_json'])
            body=response.get('body') if isinstance(response,dict) else None
            usage=body.get('usage') if isinstance(body,dict) else None
            cost=None
            overrun=False
            if isinstance(usage,dict) and all(type(usage.get(k)) is int and usage[k]>=0 for k in ('prompt_tokens','completion_tokens')):
                with localcontext() as ctx:
                    ctx.prec=100
                    cost=(Decimal(usage['prompt_tokens'])*Decimal(tariff['input_rub_per_million'])+
                        Decimal(usage['completion_tokens'])*Decimal(tariff['output_rub_per_million']))/Decimal(1000000)
                overrun=(cost>Decimal(row['reserved_rub']) or usage['prompt_tokens']>tariff['max_input_tokens']
                    or usage['completion_tokens']>tariff['max_tokens'])
            accounted=str(cost) if cost is not None else row['reserved_rub']
            state='settled' if cost is not None else 'unknown'
            if row['state']=='reconciled':
                # A late response remains auditable; a manually confirmed invoice
                # is not silently replaced with a token-based estimate.
                accounted,state=row['accounted_rub'],'reconciled'
            conn.execute('UPDATE llm_calls SET finished_at=?,response_json=?,error_json=?,state=?,accounted_rub=? WHERE call_id=?',
                (self.store.clock(),response_json,error_json,state,accounted,call_id))
            if overrun:
                conn.execute("UPDATE budget_days SET blocked=1,block_reason='Provider exceeded reserved bounds' WHERE budget_day=?",(row['budget_day'],))
        return self.get(call_id)

    def get(self, call_id):
        with self.store._transaction() as conn:
            row=self.store._require(conn.execute('SELECT * FROM llm_calls WHERE call_id=?',(call_id,)).fetchone(),'call',call_id)
            return self._row(row)

    @staticmethod
    def _row(row):
        result=dict(row)
        for name in ('request_json','tariffs_json','response_json','error_json'):
            result[name.removesuffix('_json')]=json.loads(result.pop(name)) if row[name] is not None else None
        return result

    def list_calls(self):
        with self.store._transaction() as conn:
            return [self._row(r) for r in conn.execute('SELECT * FROM llm_calls ORDER BY created_at,call_id')]

    def budget(self, day=None):
        day=day or self._day()
        with self.store._transaction() as conn:
            row=conn.execute('SELECT * FROM budget_days WHERE budget_day=?',(day,)).fetchone()
            unresolved=conn.execute("SELECT COUNT(*) FROM llm_calls WHERE state IN ('reserved','unknown') AND budget_day LIKE ?",(self.prefix+'%',)).fetchone()[0]
            with localcontext() as ctx:
                ctx.prec=100
                carry=sum((Decimal(r[0]) for r in conn.execute("SELECT accounted_rub FROM llm_calls WHERE budget_day<>? AND budget_day LIKE ? AND state IN ('reserved','unknown')",(day,self.prefix+'%'))),Decimal(0))
                accounted=self._accounted(conn,day)
                available=max(Decimal(0),Decimal(row['quota_rub'])-accounted-carry) if row else None
            return {'budget_day':day,'quota_rub':row['quota_rub'] if row else None,
                'accounted_rub':str(accounted), 'carried_reserved_rub':str(carry),
                'available_rub':str(available) if available is not None else None, 'unresolved_calls':unresolved,
                'blocked':bool(conn.execute('SELECT 1 FROM budget_days WHERE blocked=1 AND budget_day LIKE ?',(self.prefix+'%',)).fetchone())}

    def reconcile(self,call_id,actual_rub,reason):
        amount=money(actual_rub,'actual_rub')
        if not isinstance(reason,str) or not reason.strip():
            raise StorageError('invalid_argument','Reconciliation requires a non-empty reason')
        with self.store._transaction(write=True) as conn:
            row=self.store._require(conn.execute('SELECT * FROM llm_calls WHERE call_id=?',(call_id,)).fetchone(),'call',call_id)
            if row['state'] not in {'reserved','unknown'}:
                raise StorageError('invalid_call_state','Only unresolved calls can be reconciled')
            attempt=conn.execute('SELECT status,lease_expires_at FROM attempts WHERE attempt_id=?',(row['attempt_id'],)).fetchone()
            if attempt['status']=='running':
                raise StorageError('active_call','Cannot reconcile while an executor still owns the attempt')
            conn.execute("UPDATE llm_calls SET state='reconciled',accounted_rub=?,reconciliation_reason=? WHERE call_id=?",(str(amount),reason,call_id))
            if amount>Decimal(row['reserved_rub']):
                conn.execute("UPDATE budget_days SET blocked=1,block_reason='Reconciled cost exceeds reserve' WHERE budget_day=?",(row['budget_day'],))
        return self.get(call_id)

    def acknowledge_overrun(self,day,reason):
        if not isinstance(reason,str) or not reason.strip():
            raise StorageError('invalid_argument','Unblocking requires an explanation')
        with self.store._transaction(write=True) as conn:
            self.store._require(conn.execute('SELECT 1 FROM budget_days WHERE budget_day=?',(day,)).fetchone(),'budget_day',day)
            conn.execute('UPDATE budget_days SET blocked=0,block_reason=? WHERE budget_day=?',(reason,day))
        return self.budget(day)
