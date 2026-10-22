"""Frozen E0 representation contracts; vectors stay out of JSON projections."""
from __future__ import annotations
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal
import hashlib
import json
import math
import unicodedata

SCHEMA_VERSION = 'company_representations.v1'
CONTRACT_REVISION = 'e0.1'
DIMENSION = 384
BASE_VIEWS = ('business', 'finance', 'trade_geo')
GRAPH_VIEWS = ('graph_incoming', 'graph_outgoing')
BaseViewKind = Literal['business', 'finance', 'trade_geo']
ViewKind = Literal['business', 'finance', 'trade_geo', 'universal', 'graph_incoming', 'graph_outgoing', 'graph_combined', 'graph_incoming_text', 'graph_outgoing_text']


def normalize_embedding_text(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError('embedding text must be str')
    return ' '.join(unicodedata.normalize('NFC', text).split())


def json_value(value):
    if is_dataclass(value):
        return {f.name: json_value(getattr(value, f.name)) for f in fields(value) if f.metadata.get('json', True)}
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


def content_hash(value) -> str:
    return hashlib.sha256(json.dumps(json_value(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_embedding_text(text).encode()).hexdigest()


class Serializable:
    def to_dict(self):
        return json_value(self)


class RepresentationError(ValueError):
    code = 'representation_error'
    def __init__(self, message='', *, context=None, code=None):
        self.code = code or type(self).code
        self.context = context or {}
        super().__init__(message or self.code)

class InvalidProfileReference(RepresentationError): code = 'invalid_profile_reference'
class InputTooLong(RepresentationError): code = 'input_too_long'
class EncoderUnavailable(RepresentationError): code = 'encoder_unavailable'
class EncoderMismatch(RepresentationError): code = 'encoder_mismatch'
class CorruptVector(RepresentationError): code = 'corrupt_vector'
class VectorCollision(RepresentationError): code = 'vector_collision'
class ResumeMismatch(RepresentationError): code = 'resume_mismatch'
class BuildLocked(RepresentationError): code = 'build_locked'
class CompanyNotFound(RepresentationError): code = 'company_not_found'
class DependencyNotReady(RepresentationError): code = 'dependency_not_ready'
class TemporalContextUnavailable(RepresentationError): code = 'temporal_context_unavailable'


@dataclass(frozen=True)
class FeatureRef(Serializable):
    company_id: str
    profile_version: str
    feature_id: str


@dataclass(frozen=True)
class ViewPolicy(Serializable):
    version: str = 'company-views.v1'
    template_config_path: str | None = None
    template_config_hash: str | None = None
    normalization: str = 'nfc-whitespace.v1'


@dataclass(frozen=True)
class GraphPolicy(Serializable):
    enabled: bool = False
    directions: tuple[str, ...] = ('incoming', 'outgoing')
    weight_policy: str = 'sqrt_share'
    recipe_version: str = 'neighbor-business.v1'
    near_zero_norm: float = 1e-12
    hub_penalty: str = 'none'
    def __post_init__(self):
        if self.weight_policy not in {'equal', 'amount_share', 'sqrt_share'}:
            raise ValueError('unsupported graph weight policy')
        if not set(self.directions) <= {'incoming', 'outgoing'} or len(set(self.directions)) != len(self.directions):
            raise ValueError('invalid graph directions')
        if self.hub_penalty != 'none' or self.near_zero_norm <= 0:
            raise ValueError('unsupported graph policy')


@dataclass(frozen=True)
class EncoderConfig(Serializable):
    model_id: str = 'intfloat/multilingual-e5-small'
    revision: str = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
    backend: str = 'onnx'
    precision: str = 'int8'
    max_tokens: int = 512
    batch_size: int = 1
    threads: int = 2
    device_policy: str = 'cpu'
    model_dir: str | None = None
    def __post_init__(self):
        if self.max_tokens != 512 or self.batch_size < 1 or self.threads < 1:
            raise ValueError('invalid encoder limits')
        if self.device_policy not in {'cpu', 'cuda', 'auto'}:
            raise ValueError('invalid device policy')


@dataclass(frozen=True)
class BuildConfig(Serializable):
    output_root: str
    scope_ids: tuple[str, ...] | None = None
    source_manifests: dict = field(default_factory=dict)
    view_policy: ViewPolicy = field(default_factory=ViewPolicy)
    graph_policy: GraphPolicy = field(default_factory=GraphPolicy)
    encoder_config: EncoderConfig = field(default_factory=EncoderConfig)
    company_chunk_size: int = 128
    temporal_mode: str = 'strict'
    include_universal: bool = False
    def __post_init__(self):
        if not 1 <= self.company_chunk_size <= 128:
            raise ValueError('company_chunk_size must be 1..128')
        if self.temporal_mode not in {'strict', 'static_context'}:
            raise ValueError('invalid temporal mode')


@dataclass(frozen=True)
class QueryPolicy(Serializable):
    enabled_views: tuple[str, ...] = BASE_VIEWS
    graph_enabled: bool = False
    graph_policy: GraphPolicy = field(default_factory=GraphPolicy)
    temporal_mode: str = 'strict'
    def __post_init__(self):
        if self.temporal_mode not in {'strict', 'static_context'}:
            raise ValueError('invalid temporal mode')


@dataclass(frozen=True)
class VariantConfig(Serializable):
    variant_id: str
    dense_views: tuple[str, ...] = BASE_VIEWS
    hybrid: bool = True
    encoder_fingerprint: str = ''
    index_snapshot_id: str = ''
    corpus_hash: str = ''
    profile_snapshot_id: str = ''
    split: str = 'test'
    role: str = 'cfo'
    as_of: str = ''
    window_start: str = ''
    window_end: str = ''
    top_k_per_view: int = 50
    union_cap: int = 250
    pool_cap: int = 100
    output_limit: int = 20
    token_budget: int = 8192
    counter_version: str = 'utf8_bytes_div4.v1'
    fusion_k: int = 60
    channel_weights: tuple[float, float, float] = (1., 1., 1.)
    reserve_policy: str = 'none'
    temporal_mode: str = 'strict'
    graph_weight_policy: str = 'sqrt_share'
    def __post_init__(self):
        if not 1 <= self.top_k_per_view <= 50 or not 1 <= self.output_limit <= 20 or not 1 <= self.pool_cap <= self.union_cap <= 250 or self.pool_cap>100:
            raise ValueError('invalid retrieval caps')
        if len(self.channel_weights)!=3 or any(not math.isfinite(w) or w<0 for w in self.channel_weights):
            raise ValueError('invalid channel weights')
        if self.token_budget<1 or self.fusion_k<1 or self.temporal_mode not in {'strict','static_context'}:
            raise ValueError('invalid retrieval policy')


@dataclass(frozen=True)
class CompanyView(Serializable):
    company_id: str
    profile_version: str
    kind: str
    text: str
    text_hash: str
    content_hash: str
    template_version: str
    feature_refs: list[FeatureRef] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)
    search_topics: list[dict] = field(default_factory=list)
    quality: str = 'observed'
    unknowns: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    source_provenance: dict = field(default_factory=dict)
    def __post_init__(self):
        if not normalize_embedding_text(self.text) or self.text != normalize_embedding_text(self.text):
            raise ValueError('view text must be nonempty and normalized')
        if self.text_hash != text_hash(self.text):
            raise ValueError('view text_hash mismatch')


@dataclass(frozen=True)
class CompanyViews(Serializable):
    company_id: str
    profile_version: str
    views: dict[str, CompanyView] = field(default_factory=dict)
    omitted: dict[str, str] = field(default_factory=dict)
    def __post_init__(self):
        if set(self.views) & set(self.omitted) or set(self.views) | set(self.omitted) != set(BASE_VIEWS):
            raise ValueError('exactly one view or omission per base kind required')


@dataclass(frozen=True)
class PaymentDatasetManifest(Serializable):
    source_path: str
    source_sha256: str
    policy_version: str = 'exclude-all-repeated-directed-pairs-v1'
    currency: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    available_at: str | None = None
    metadata_source: dict | None = None


@dataclass(frozen=True)
class PaymentEdge(Serializable):
    edge_id: str
    payer_id: str
    beneficiary_id: str
    amount_raw: str
    payment_count: int
    source_record_numbers: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class Neighbor(Serializable):
    company_id: str
    direction: str
    edge_id: str
    amount_raw: str
    payment_count: int
    observed_share: float
    profile_status: str
    business_view_content_hash: str | None = None
    profile_version: str | None = None
    feature_refs: list[FeatureRef] = field(default_factory=list)


@dataclass(frozen=True)
class DirectionCoverage(Serializable):
    retained_neighbor_count: int = 0
    usable_profile_count: int = 0
    business_vector_count: int | None = None
    retained_amount_raw: str = '0'
    usable_profile_amount_raw: str = '0'
    vector_backed_amount_raw: str | None = None
    known_node_fraction: float | None = None
    known_amount_fraction: float | None = None
    vector_node_fraction: float | None = None
    vector_amount_fraction: float | None = None
    retained_payment_count: int = 0
    known_payment_count: int = 0
    cleaning_coverage: dict = field(default_factory=dict)
    share_quality: str = 'unavailable'


@dataclass(frozen=True)
class PaymentNeighborhood(Serializable):
    company_id: str
    dataset_manifest: PaymentDatasetManifest
    graph_content_hash: str
    provenance_hash: str
    incoming: list[Neighbor] = field(default_factory=list)
    outgoing: list[Neighbor] = field(default_factory=list)
    coverage: dict[str, DirectionCoverage] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    sector_unknown_totals: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SectorAggregate(Serializable):
    direction: str
    okved_code: str
    okved_name: str
    neighbor_count: int
    amount_raw: str
    payment_count: int
    observed_amount_share: float
    neighbor_ids: list[str] = field(default_factory=list)
    edge_ids: list[str] = field(default_factory=list)
    neighbor_feature_refs: list[FeatureRef] = field(default_factory=list)


@dataclass(frozen=True)
class EncodedView(Serializable):
    company_id: str
    kind: str
    view_content_hash: str
    vector_key: str
    encoder_fingerprint: str
    vector_sha256: str
    profile_version: str
    feature_refs: list[FeatureRef] = field(default_factory=list)
    metadata_ref: Any = None
    vector: Any = field(default=None, repr=False, compare=False, metadata={'json': False})


@dataclass(frozen=True)
class EncodedGraphView(EncodedView):
    recipe_version: str = 'neighbor-business.v1'
    direction: str = ''
    weight_policy: str = 'sqrt_share'
    graph_content_hash: str = ''
    input_vector_refs: list[dict] = field(default_factory=list)
    coverage: DirectionCoverage = field(default_factory=DirectionCoverage)
    normalized_weights_ref: Any = None
    norm_before_normalization: float = 0.
    limitations: list[str] = field(default_factory=list)
    representation_type: str = 'aggregation'


@dataclass(frozen=True)
class GraphViews(Serializable):
    views: dict[str, EncodedGraphView] = field(default_factory=dict)
    omitted: dict[str, str] = field(default_factory=dict)
    coverage: dict[str, DirectionCoverage] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphBuildReport(Serializable):
    path: str
    status: str = 'complete'
    statistics: dict = field(default_factory=dict)
    source_sha256: str = ''


@dataclass(frozen=True)
class BuildReport(Serializable):
    snapshot_id: str = ''
    snapshot_path: str = ''
    status: str = 'complete'
    statistics: dict = field(default_factory=dict)
    omitted: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


@dataclass(frozen=True)
class VerificationReport(Serializable):
    valid: bool
    errors: list = field(default_factory=list)
    statistics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotKey(Serializable):
    event_id: str
    event_version: int
    payload_sha256: str
    def __post_init__(self):
        if type(self.event_version) is not int or self.event_version < 1:
            raise ValueError('event_version must be positive integer')


@dataclass(frozen=True)
class ChunkRef(Serializable):
    snapshot: SnapshotKey
    chunk_id: str
    claim_refs: list[dict] = field(default_factory=list)
    offsets: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class CompanyQuerySet(Serializable):
    company_id: str
    profile_version: str
    snapshot_id: str
    queries: list[EncodedView] = field(default_factory=list)
    omitted: dict[str, str] = field(default_factory=dict)
    payment_context_ref: Any = None
    temporal_mode: str = 'strict'
    limitations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DenseHit(Serializable):
    snapshot: SnapshotKey
    view_kind: str
    view_ref: Any
    chunk_ref: ChunkRef
    rank: int
    raw_score: float
    company_feature_refs: list[FeatureRef] = field(default_factory=list)
    graph_evidence_ref: Any = None
    channel: str = 'dense'
