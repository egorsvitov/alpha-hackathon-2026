"""Explicit offline/live runtime with one persistent call quota for merge and analyst."""
from uuid import uuid4

from app.analyst.llm_gateway import BaseLLM, LLMGateway, BudgetLedger, BudgetExceeded
from app.analyst.service import AnalystService
from app.analyst.settings import AnalystSettings
from llm.cluster_merge import MergeService, OpenAIProvider, Profile, SQLiteQuota
from llm.cluster_merge.provider import ProviderError
from retriever.merge_adapter import ClusterMergeAdapter
from .storage import PipelineError


class QuotaLLM(BaseLLM):
    def __init__(self, gateway, quota, root):
        super().__init__(ledger=gateway.ledger,stats=gateway.stats,max_json_repairs=0)
        self.gateway, self.quota, self.root = gateway, quota, root

    def chat(self, request):
        try:
            self.quota.reserve('analyst:' + uuid4().hex)
        except ProviderError:
            raise BudgetExceeded('Общий лимит вызовов исчерпан') from None
        # Gateway logs are content-free. Private raw responses are journaled here,
        # including responses which subsequently fail JSON/QA checks.
        result = self.gateway.chat(request)
        from .ingest import atomic_json
        from dataclasses import asdict
        atomic_json(self.root/'analyst-responses'/uuid4().hex, dict(request=asdict(request),response=asdict(result)))
        return result


def analyst_runtime(workspace, *, live=False, quota_path=None, quota_limit=None, budget_rub=None):
    base = AnalystSettings.from_env()
    settings = base.with_overrides(mode='llm' if live else 'cached',
        runtime_cache_dir=workspace.root/'analyst-cache',bundled_cache_dir=workspace.root/'no-bundled-cache',
        memory_enabled=False, max_parallel=1, fallback_model='', fallback_base_url='',
        max_retries=0,max_json_repairs=0,prompt_version='analyst-v1.2')
    if not live:
        return AnalystService(settings,bank_evidence=[]),None
    if not base.has_key:
        raise PipelineError('live_api_key_required')
    if not quota_path or not quota_limit or quota_limit<1 or budget_rub is None or budget_rub<=0:
        raise PipelineError('live_shared_quota_and_budget_required')
    quota = SQLiteQuota(quota_path,quota_limit)
    ledger = BudgetLedger(budget_rub,session_cap_rub=budget_rub,state_path=workspace.root/'analyst-budget.json')
    gateway = LLMGateway(api_key=base.api_key,base_url=base.base_url,fallback_base_url='',ledger=ledger,
                         max_retries=0,max_json_repairs=0,timeout_s=base.timeout_s,check_key_info=base.check_key_info)
    return AnalystService(settings,llm=QuotaLLM(gateway,quota,workspace.root),bank_evidence=[]),quota


def merge_runtime(workspace, *, live=False, quota=None):
    if not live:
        # Can consume completed cached operations without sending requests.
        class NoCalls:
            def reserve(self,key):
                raise ProviderError('offline_calls_disabled')
        quota = NoCalls()
    elif quota is None:
        raise PipelineError('live_shared_quota_required')
    return ClusterMergeAdapter(MergeService(OpenAIProvider(),quota,workspace.root/'merge.sqlite3'),Profile())


class LazyCPUEncoder:
    """Declared index profile; actual model/parity must verify before any new vector.

    An unchanged corpus/job must survive restart without loading weights or
    re-encoding the parity samples. No unverified computed vector is returned.
    """
    def __init__(self, reference, model_path, output, *, fingerprint, dimension, threads=2):
        self.reference, self.model_path, self.output = reference, model_path, output
        self.fingerprint, self.dimension, self.threads = fingerprint, dimension, threads
        self._encoder = None

    def encode_event_texts(self, texts):
        if self._encoder is None:
            from retriever.cpu_encoder import CPUEncoder
            candidate = CPUEncoder(self.reference,self.model_path,self.output,threads=self.threads)
            if (candidate.fingerprint,candidate.dimension) != (self.fingerprint,self.dimension):
                raise PipelineError('encoder_profile_mismatch')
            self._encoder = candidate
        return self._encoder.encode_event_texts(texts)
