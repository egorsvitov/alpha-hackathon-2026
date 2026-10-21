"""Economic event contracts, extraction and auditable normalization (B1–B5)."""

from .models import ArticleInput, ArticleRecord, EventBundle
from .validation import ContractValidationError, validate_bundle, validate_grounding
from .storage import EventStore, StorageError
from .storage_models import ProcessingSpec
from .extraction import build_processing_spec, parse_extraction
from .extraction_models import ProviderProfile, ExecutionLimits, ExtractionResponse, ExtractionResult
from .runner import ExtractionRunner
from .normalization import normalize_extraction, validate_normalized_result
from .normalization_models import NormalizationConfig, NormalizedArticleResult
from .normalization_store import NormalizationService

from .catalog_models import CatalogConfig, ClaimContext, IdentityDescriptor, PairDecision, CanonicalEventRecord, ReviewRequest
from .catalog_store import CatalogService
from .matching import describe_identity, compare_claims
from .catalog_validation import validate_catalog_record

__all__ = [
    "ArticleInput", "ArticleRecord", "EventBundle", "ContractValidationError",
    "validate_bundle", "validate_grounding",
    "EventStore", "StorageError", "ProcessingSpec",
    "build_processing_spec", "parse_extraction", "ProviderProfile", "ExecutionLimits",
    "ExtractionResponse", "ExtractionResult", "ExtractionRunner",
    "normalize_extraction", "validate_normalized_result", "NormalizationConfig",
    "NormalizedArticleResult", "NormalizationService",
    "CatalogService", "CatalogConfig", "ClaimContext", "IdentityDescriptor", "PairDecision",
    "CanonicalEventRecord", "ReviewRequest", "describe_identity", "compare_claims", "validate_catalog_record",
]
