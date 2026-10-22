"""Lazy backwards-compatible public exports for the original pipeline."""

from importlib import import_module

_EXPORTS = {
    "ArticleInput": "llm.events.contracts.models",
    "ArticleRecord": "llm.events.contracts.models",
    "EventBundle": "llm.events.contracts.models",
    "ContractValidationError": "llm.events.contracts.validation",
    "validate_bundle": "llm.events.contracts.validation",
    "validate_grounding": "llm.events.contracts.validation",
    "EventStore": "llm.events.storage.store",
    "StorageError": "llm.events.storage.store",
    "ProcessingSpec": "llm.events.storage.models",
    "build_processing_spec": "llm.events.extraction.protocol",
    "parse_extraction": "llm.events.extraction.protocol",
    "parse_provider_extraction": "llm.events.extraction.protocol",
    "ProviderProfile": "llm.events.extraction.models",
    "ExecutionLimits": "llm.events.extraction.models",
    "ExtractionResponse": "llm.events.extraction.models",
    "ExtractionResult": "llm.events.extraction.models",
    "ExtractionRunner": "llm.events.extraction.runner",
    "normalize_extraction": "llm.events.normalization.core",
    "validate_normalized_result": "llm.events.normalization.core",
    "NormalizationConfig": "llm.events.normalization.models",
    "NormalizedArticleResult": "llm.events.normalization.models",
    "NormalizationService": "llm.events.normalization.service",
    "CatalogConfig": "llm.events.catalog.models",
    "ClaimContext": "llm.events.catalog.models",
    "IdentityDescriptor": "llm.events.catalog.models",
    "PairDecision": "llm.events.catalog.models",
    "CanonicalEventRecord": "llm.events.catalog.models",
    "ReviewRequest": "llm.events.catalog.models",
    "CatalogService": "llm.events.catalog.service",
    "describe_identity": "llm.events.catalog.matching",
    "compare_claims": "llm.events.catalog.matching",
    "validate_catalog_record": "llm.events.catalog.validation",
    "EventBundleService": "llm.events.publication.service",
    "build_bundle": "llm.events.publication.builder",
    "PublicationConfig": "llm.events.publication.models",
    "PublicationContext": "llm.events.publication.models",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(_EXPORTS[name]), name)
    globals()[name] = value
    return value
