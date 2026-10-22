"""Experimental evidence-first pipeline, independent of SourceClaim/B6."""

from .models import (
    SourceBlock,
    EventMention,
    ExtractionAudit,
    MembershipDecision,
    EventCluster,
    RunManifest,
)
from .blocks import source_blocks, packets
from .extraction import parse_mentions
from .retrieval import BM25Retriever
from .clustering import parse_decisions, attachment

_PUBLIC = {
    "plan_run",
    "extract_mentions",
    "retrieve_candidates",
    "cluster_mentions",
    "resume_run",
}


def __getattr__(name):
    if name in _PUBLIC:
        from . import pipeline

        return getattr(pipeline, name)
    if name in {"export_run", "evaluate_run"}:
        from . import output

        return getattr(output, name)
    raise AttributeError(name)


__all__ = [
    "SourceBlock",
    "EventMention",
    "ExtractionAudit",
    "MembershipDecision",
    "EventCluster",
    "RunManifest",
    "source_blocks",
    "packets",
    "parse_mentions",
    "BM25Retriever",
    "parse_decisions",
    "attachment",
    "plan_run",
    "extract_mentions",
    "retrieve_candidates",
    "cluster_mentions",
    "resume_run",
    "export_run",
    "evaluate_run",
]
