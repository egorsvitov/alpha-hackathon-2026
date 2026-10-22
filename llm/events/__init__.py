"""Economic event contracts, extraction and auditable normalization (B1–B6)."""

from llm.events.contracts.models import ArticleInput, ArticleRecord, EventBundle
from llm.events.contracts.validation import ContractValidationError, validate_bundle, validate_grounding
from llm.events.storage.store import EventStore, StorageError
from llm.events.storage.models import ProcessingSpec
from llm.events.extraction.protocol import build_processing_spec, parse_extraction, parse_provider_extraction
from llm.events.extraction.models import ProviderProfile, ExecutionLimits, ExtractionResponse, ExtractionResult
from llm.events.extraction.runner import ExtractionRunner
from llm.events.normalization.core import normalize_extraction, validate_normalized_result
from llm.events.normalization.models import NormalizationConfig, NormalizedArticleResult
from llm.events.normalization.service import NormalizationService

from llm.events.catalog.models import CatalogConfig, ClaimContext, IdentityDescriptor, PairDecision, CanonicalEventRecord, ReviewRequest
from llm.events.catalog.service import CatalogService
from llm.events.catalog.matching import describe_identity, compare_claims
from llm.events.catalog.validation import validate_catalog_record
from llm.events.publication.service import EventBundleService
from llm.events.publication.builder import build_bundle
from llm.events.publication.models import PublicationConfig, PublicationContext

__all__ = [
    "ArticleInput", "ArticleRecord", "EventBundle", "ContractValidationError",
    "validate_bundle", "validate_grounding",
    "EventStore", "StorageError", "ProcessingSpec",
    "build_processing_spec", "parse_extraction", "parse_provider_extraction",
    "ProviderProfile", "ExecutionLimits",
    "ExtractionResponse", "ExtractionResult", "ExtractionRunner",
    "normalize_extraction", "validate_normalized_result", "NormalizationConfig",
    "NormalizedArticleResult", "NormalizationService",
    "CatalogService", "CatalogConfig", "ClaimContext", "IdentityDescriptor", "PairDecision",
    "CanonicalEventRecord", "ReviewRequest", "describe_identity", "compare_claims", "validate_catalog_record",
    "EventBundleService", "build_bundle", "PublicationConfig", "PublicationContext",
]
