"""``POST /v1/search`` - the hybrid retrieval pipeline over REST. Owner: A09.

The request body *is* :class:`~aimemory.domain.retrieval.SearchQuery` and the response *is*
:class:`~aimemory.domain.retrieval.SearchResult`: the frozen P1 contract is the wire format, so the
MCP server (A10) and the evaluation harness (A12) deserialize the same shape the Gateway produced,
with no REST-only translation layer to drift.

A degraded dependency is a 200 with ``warnings``, never a 5xx: an answer the caller can see is
qualified is strictly better than an error (plan section Y failure tests).
"""

from __future__ import annotations

from typing import Annotated

from aimemory.domain.retrieval import SearchQuery, SearchResult
from aimemory.gateway import Gateway
from deps import get_gateway
from fastapi import APIRouter, Depends
from metrics import COUNTERS

router = APIRouter(prefix="/v1", tags=["search"])


@router.post(
    "/search",
    response_model=SearchResult,
    summary="Hybrid search (vector + keyword + graph) with assembled context",
)
def search(
    query: SearchQuery,
    gateway: Annotated[Gateway, Depends(get_gateway)],
) -> SearchResult:
    result = gateway.search(query)
    COUNTERS.record_warnings(result.warnings)
    return result
