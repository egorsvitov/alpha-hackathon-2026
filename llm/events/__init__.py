"""Economic event contracts, durable state and unverified LLM extraction (B1–B3)."""

from .models import ArticleInput, ArticleRecord, EventBundle
from .validation import ContractValidationError, validate_bundle, validate_grounding
from .storage import EventStore, StorageError
from .storage_models import ProcessingSpec
from .extraction import build_processing_spec, parse_extraction
from .extraction_models import ProviderProfile, ExecutionLimits, ExtractionResponse, ExtractionResult
from .runner import ExtractionRunner

__all__ = [
    "ArticleInput", "ArticleRecord", "EventBundle", "ContractValidationError",
    "validate_bundle", "validate_grounding",
    "EventStore", "StorageError", "ProcessingSpec",
    "build_processing_spec", "parse_extraction", "ProviderProfile", "ExecutionLimits",
    "ExtractionResponse", "ExtractionResult", "ExtractionRunner",
]
