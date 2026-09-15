"""Hybrid retrieval (plan section P, ``docs/architecture/retrieval.md``). Owner: A09.

P9-T01 implements stages 1, 2, 3 and 6:

===============================================  =========================================
Stage                                            Module
===============================================  =========================================
0. configuration (``config/retrieval.yaml``)     :mod:`aimemory.retrieval.config`
   SQL scoping shared by both retrievers         :mod:`aimemory.retrieval.filters`
1. semantic candidates (pgvector HNSW cosine)    :mod:`aimemory.retrieval.candidates`
2. keyword candidates (tsvector, ``ts_rank_cd``) :mod:`aimemory.retrieval.candidates`
3. Reciprocal Rank Fusion (``rrf_k`` = 60)       :mod:`aimemory.retrieval.fusion`
6. boosts + deterministic final ranking          :mod:`aimemory.retrieval.boosts`
   orchestration + degraded modes                :mod:`aimemory.retrieval.pipeline`
===============================================  =========================================

Stages 4 (graph expansion), the fact side of 5 (temporal filter), 7 (provenance) and 8 (context
assembly) are P10-T01 and are not in this package yet. :class:`aimemory.retrieval.types.RankedCandidate`
is the seam: it carries every field a :class:`~aimemory.domain.retrieval.ScoredHit` needs and becomes
one through :meth:`~aimemory.retrieval.types.RankedCandidate.to_scored_hit` once P10 has resolved
provenance.

Two documented departures from the letter of ``retrieval.md``, both reported with P9-T01:

* ``since`` is evaluated against ``source_versions.observed_at`` for chunks, not
  ``chunks.created_at``. ``temporal.md`` §8 and its deviation 5 define ``since`` on the observation
  axis; ``chunks.created_at`` is ingestion time, which would answer a different question.
* The query vector is bound as a text literal and cast in SQL rather than adapted by psycopg -
  see the module docstring of :mod:`aimemory.retrieval.candidates` for why a ``list[float]`` cannot
  be an operand of ``<=>``.
"""

from .boosts import apply_boosts, compute_boosts, rank_candidates, recency_boost
from .candidates import keyword_candidates, semantic_candidates
from .config import load_retrieval_config
from .filters import ScopeFilters
from .fusion import reciprocal_rank_fusion, rrf_contribution
from .pipeline import HybridRetriever, RetrievalOutcome
from .types import HitKey, HitMetadata, RankedCandidate, RetrieverOutput, ScoredCandidate

__all__ = [
    "HitKey",
    "HitMetadata",
    "HybridRetriever",
    "RankedCandidate",
    "RetrievalOutcome",
    "RetrieverOutput",
    "ScopeFilters",
    "ScoredCandidate",
    "apply_boosts",
    "compute_boosts",
    "keyword_candidates",
    "load_retrieval_config",
    "rank_candidates",
    "recency_boost",
    "reciprocal_rank_fusion",
    "rrf_contribution",
    "semantic_candidates",
]
