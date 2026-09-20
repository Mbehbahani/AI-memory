"""Hybrid retrieval (plan section P, ``docs/architecture/retrieval.md``). Owner: A09.

All eight stages of the spec live here; the Gateway (:mod:`aimemory.gateway`) composes them and the
REST layer (``apps/memory-api``) exposes them.

===============================================  =========================================
Stage                                            Module
===============================================  =========================================
0. configuration (``config/retrieval.yaml``)     :mod:`aimemory.retrieval.config`
   SQL scoping shared by both retrievers         :mod:`aimemory.retrieval.filters`
1. semantic candidates (pgvector HNSW cosine)    :mod:`aimemory.retrieval.candidates`
2. keyword candidates (tsvector, ``ts_rank_cd``) :mod:`aimemory.retrieval.candidates`
3. Reciprocal Rank Fusion (``rrf_k`` = 60)       :mod:`aimemory.retrieval.fusion`
4. graph expansion (Neo4j, 1 hop, bounded)       :mod:`aimemory.retrieval.expansion`
5. temporal filter (``as_of`` / ``since``)       :mod:`aimemory.retrieval.temporal`
5b. calibrated relevance (cosine + IDF cover)    :mod:`aimemory.retrieval.relevance`
6. boosts + deterministic final ranking          :mod:`aimemory.retrieval.boosts`
7. provenance attachment + citations             :mod:`aimemory.retrieval.provenance`
8. context assembly (ordered, budgeted)          :mod:`aimemory.retrieval.context`
9. ``retrieval_logs``                            :mod:`aimemory.retrieval.logs`
   orchestration + degraded modes                :mod:`aimemory.retrieval.pipeline`
===============================================  =========================================

:class:`~aimemory.retrieval.types.RankedCandidate` is the seam between stages 6 and 7: it carries
every field a :class:`~aimemory.domain.retrieval.ScoredHit` needs and becomes one through
:meth:`~aimemory.retrieval.types.RankedCandidate.to_scored_hit`, which is the *only* place
provenance and a citation are attached.

Both degraded modes are mandatory behaviour, not best effort, and both are proved by tests that
simulate the outage:

* **embedding service down** -> keyword-only with
  :data:`~aimemory.retrieval.pipeline.EMBEDDING_DEGRADED_WARNING`;
* **Neo4j down** -> vector+keyword with
  :data:`~aimemory.retrieval.expansion.GRAPH_DEGRADED_WARNING`.

One documented departure from the letter of ``retrieval.md`` remains: the query vector is bound as a
text literal and cast in SQL rather than adapted by psycopg - see the module docstring of
:mod:`aimemory.retrieval.candidates` for why a ``list[float]`` cannot be an operand of ``<=>``.
"""

from .boosts import apply_boosts, compute_boosts, rank_candidates, recency_boost
from .candidates import keyword_candidates, semantic_candidates
from .config import load_retrieval_config
from .context import ContextInputs, DecisionRow, ProjectSummary, assemble_context, estimate_tokens
from .expansion import GRAPH_DEGRADED_WARNING, GraphExpansion, expand, seed_entity_ids
from .filters import ScopeFilters
from .fusion import reciprocal_rank_fusion, rrf_contribution
from .logs import effective_params, write_retrieval_log
from .pipeline import EMBEDDING_DEGRADED_WARNING, HybridRetriever, RetrievalOutcome
from .staleness import DISABLED_ROOT_WARNING, STALE_CORPUS_WARNING, staleness_warnings
from .provenance import attach_provenance, load_provenance, render_citation
from .relevance import (
    LEXICAL_DEGRADED_WARNING,
    QueryLexicon,
    RelevanceScore,
    analyse_query,
    lexical_coverage,
    score_relevance,
)
from .temporal import FactRow, current_facts, facts_for_entities, filter_hits
from .types import HitKey, HitMetadata, RankedCandidate, RetrieverOutput, ScoredCandidate

__all__ = [
    "EMBEDDING_DEGRADED_WARNING",
    "DISABLED_ROOT_WARNING",
    "GRAPH_DEGRADED_WARNING",
    "STALE_CORPUS_WARNING",
    "staleness_warnings",
    "LEXICAL_DEGRADED_WARNING",
    "ContextInputs",
    "DecisionRow",
    "FactRow",
    "GraphExpansion",
    "HitKey",
    "HitMetadata",
    "HybridRetriever",
    "ProjectSummary",
    "QueryLexicon",
    "RankedCandidate",
    "RelevanceScore",
    "RetrievalOutcome",
    "RetrieverOutput",
    "ScopeFilters",
    "ScoredCandidate",
    "analyse_query",
    "apply_boosts",
    "assemble_context",
    "attach_provenance",
    "compute_boosts",
    "current_facts",
    "effective_params",
    "estimate_tokens",
    "expand",
    "facts_for_entities",
    "filter_hits",
    "keyword_candidates",
    "lexical_coverage",
    "load_provenance",
    "load_retrieval_config",
    "rank_candidates",
    "recency_boost",
    "reciprocal_rank_fusion",
    "render_citation",
    "rrf_contribution",
    "score_relevance",
    "seed_entity_ids",
    "semantic_candidates",
    "write_retrieval_log",
]
