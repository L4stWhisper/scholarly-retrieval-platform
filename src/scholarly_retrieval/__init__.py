"""Public package surface for the scholarly retrieval core."""

from .models import (
    ExpansionDirection,
    GraphExpansionQuery,
    GraphExpansionResult,
    GraphFilterExclusion,
    GraphFilterReason,
    GraphNodeRanking,
    GraphResult,
    GraphSimilarity,
    GraphSimilarityKind,
    Paper,
    RelatedQuery,
    RelatedResult,
    ResolveResult,
    SearchQuery,
    SearchResult,
    StopRule,
)
from .service import ScholarService

__all__ = [
    "ExpansionDirection",
    "GraphResult",
    "GraphExpansionQuery",
    "GraphExpansionResult",
    "GraphFilterExclusion",
    "GraphFilterReason",
    "GraphNodeRanking",
    "GraphSimilarity",
    "GraphSimilarityKind",
    "Paper",
    "RelatedQuery",
    "RelatedResult",
    "ResolveResult",
    "ScholarService",
    "SearchQuery",
    "SearchResult",
    "StopRule",
]
__version__ = "0.1.0"
