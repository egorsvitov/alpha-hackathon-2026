"""Public standalone retrieval API and version-one contracts."""
from .merge_adapter import ClusterMergeAdapter
from .models import (
    ApplyResult, ArticleRevision, Candidate, ClusterSnapshot, CompanyInterestSet,
    EncodedItem, IndexSnapshot, Interest, RetrievalCandidateBatch, RetrievalRequest,
    RetrievalRun, SearchHit, SearchItem, VectorRecord,
)
from .selection import RetrieverService
from .store import SQLiteSearchStore

__all__ = [
    'RetrieverService', 'SQLiteSearchStore', 'ClusterMergeAdapter',
    'ApplyResult', 'ArticleRevision', 'Candidate', 'ClusterSnapshot',
    'CompanyInterestSet', 'EncodedItem', 'IndexSnapshot', 'Interest',
    'RetrievalCandidateBatch', 'RetrievalRequest', 'RetrievalRun', 'SearchHit',
    'SearchItem', 'VectorRecord',
]
