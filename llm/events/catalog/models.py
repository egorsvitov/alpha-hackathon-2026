"""B5 internal contracts. These are not the B6 transport contract."""
from typing import Annotated, Literal
from pydantic import Field
from llm.events.contracts.models import ContractModel, Text, Timestamp, SourceClaim, Source, Evidence, EventType, Conflict, Relation
from llm.events.normalization.models import Diagnostic, Transformation

Identity = Literal['same_event', 'different_event', 'uncertain']
ContentRelation = Literal['duplicate_claim', 'complements', 'conflicts_with', 'updates', 'none']


class CatalogConfig(ContractModel):
    rules_version: Literal['b5.1'] = 'b5.1'
    top_k: Annotated[int, Field(ge=1)] = 20
    max_claims: Annotated[int, Field(ge=1)] = 10000
    max_comparisons: Annotated[int, Field(ge=1)] = 1000000


class ClaimContext(ContractModel):
    artifact_id: Text
    candidate_id: Text
    event_type: EventType
    claim: SourceClaim
    source: Source
    evidence: list[Evidence]
    transformations: list[Transformation] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class IdentityBasis(ContractModel):
    feature: Text
    path: Text
    evidence_ids: list[Text]
    transformation_paths: list[Text] = Field(default_factory=list)
    # Literal pattern matches refer to immutable evidence spans.
    source_id: str | None = None
    field: Literal['title', 'text'] | None = None
    start: int | None = None
    end: int | None = None


class IdentityDescriptor(ContractModel):
    claim_id: Text
    event_type: EventType
    features: dict[str, str]
    missing: list[str]
    bases: list[IdentityBasis]


class FieldComparison(ContractModel):
    left_path: Text
    right_path: Text


class PairDecision(ContractModel):
    schema_version: Literal['b5.1'] = 'b5.1'
    left_id: Text
    right_id: Text
    identity: Identity
    content: ContentRelation = 'none'
    origin: Literal['rules', 'manual', 'llm'] = 'rules'
    status: Literal['effective', 'recommendation'] = 'effective'
    rules_hash: Text
    reasons: list[Text]
    bases: list[IdentityBasis] = Field(default_factory=list)
    fields: list[FieldComparison] = Field(default_factory=list)
    hard_differences: list[Text] = Field(default_factory=list)
    retrieval_methods: list[Text] = Field(default_factory=list)
    retrieval_score: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0
    # Direction is explicit for updates: new -> old.
    from_id: str | None = None
    to_id: str | None = None


class CanonicalEventRecord(ContractModel):
    schema_version: Literal['b5.1'] = 'b5.1'
    catalog_id: Text
    event_id: Text
    event_version: Annotated[int, Field(ge=1)]
    event_type: EventType
    availability: Literal['active', 'withdrawn']
    updated_at: Timestamp
    anchor_claim_id: Text
    claims: list[SourceClaim]
    sources: list[Source]
    evidence: list[Evidence]
    conflicts: list[Conflict]
    relations: list[Relation]
    limitations: list[Text]
    decision_ids: list[Text]
    replaced_by: list[Text] = Field(default_factory=list)
    semantic_support: Literal['not_assessed'] = 'not_assessed'


class ReviewRequest(ContractModel):
    left_id: Text
    right_id: Text
    identity: Identity
    content: ContentRelation = 'none'
    reason: Text
    fields: list[FieldComparison] = Field(default_factory=list)
    from_id: str | None = None
    to_id: str | None = None


class PairRecommendation(ContractModel):
    identity: Identity
    content: ContentRelation
    reason: Text
    # References must resolve in the two supplied contexts.
    evidence_ids: Annotated[list[Text], Field(min_length=1)]
    fields: list[FieldComparison]


class CatalogSnapshot(ContractModel):
    schema_version: Literal['b5.1'] = 'b5.1'
    catalog_id: Text
    revision: Annotated[int, Field(ge=1)]
    events: list[CanonicalEventRecord]
    decisions: list[PairDecision]


class PairCheckResult(ContractModel):
    schema_version: Literal['b5.1'] = 'b5.1'
    verification_status: Literal['recommendation_only'] = 'recommendation_only'
    processing_spec_hash: Text
    pair: Annotated[list[ClaimContext], Field(min_length=2, max_length=2)]
    recommendation: PairRecommendation
