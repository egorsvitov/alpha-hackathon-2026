"""Single owner of agent boundary types; no provider implementation lives here."""
from dataclasses import dataclass, field, asdict
from typing import Any, Protocol
from ranking.contracts import Clock, SystemClock

@dataclass(frozen=True)
class GenerationRequest:
    purpose: str
    run_id: str
    attempt_id: str
    model_id: str
    prompt_version: str
    structured_payload: dict
    output_schema: dict
    max_output_tokens: int = 2048
    messages: tuple = ()

@dataclass(frozen=True)
class GenerationResult:
    payload: dict
    model_id: str = 'offline-fixture-v1'
    provider_request_id: str | None = None
    mode: str = 'fixture'
    usage: dict = field(default_factory=dict)
    usage_status: str = 'unknown'
    finish_reason: str = 'stop'
    elapsed_ms: int = 0

@dataclass(frozen=True)
class QAReport:
    status: str = 'limited'
    next_action: str = 'deliver'
    issues: list = field(default_factory=list)
    checks_run: list = field(default_factory=list)
    checks_not_run: list = field(default_factory=lambda: ['independent_semantic_review'])
    missing_evidence_queries: list = field(default_factory=list)
    structural_status: str = 'passed'
    semantic_status: str = 'not_run'
    publication_status: str = 'limited'
    def to_dict(self):
        return asdict(self)

class Generator(Protocol):
    def generate(self, request: GenerationRequest) -> GenerationResult: ...

class BudgetPort(Protocol):
    def reserve(self, request_fingerprint, model_id, input_bound, max_output_tokens, purpose, module_id): ...
    def mark_dispatched(self, reservation_id): ...
    def settle(self, reservation_id, usage): ...
    def mark_uncertain(self, reservation_id, reason): ...
    def cancel_unsent(self, reservation_id): ...

@dataclass
class Services:
    event_store: Any = None
    profile_repository: Any = None
    retriever: Any = None
    generator: Any = None
    qa: Any = None
    run_store: Any = None
    budget: Any = None
    config: dict = field(default_factory=dict)
    clock: Clock = field(default_factory=SystemClock)
    corpus: Any = None
