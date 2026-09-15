"""Stages 1-3 and 6 wired together: the hybrid candidate pipeline (P9-T01).

``HybridRetriever.retrieve`` is the part of ``docs/architecture/retrieval.md`` that P9-T01 owns::

    SearchQuery -> semantic Candidate[]  (pgvector HNSW)
                -> keyword  Candidate[]  (tsvector)
                -> RRF fusion            -> ScoredCandidate[]   (fused_top_k)
                -> boosts + rank         -> RankedCandidate[]   (final_k)

Graph expansion (stage 4), the fact/entity side of the temporal filter (stage 5), provenance
attachment (stage 7) and context assembly (stage 8) are P10-T01 and are deliberately absent here:
:class:`~aimemory.retrieval.types.RankedCandidate` carries everything they need and converts to the
frozen :class:`~aimemory.domain.retrieval.ScoredHit` in one place.

Degradation (plan section Y failure tests): the embedding service being unreachable downgrades the
query to **keyword-only with a warning** instead of failing it. The warning travels in
:attr:`RetrievalOutcome.warnings`, which the Gateway copies into ``SearchResult.warnings`` - a
degraded answer the caller cannot see is worse than no answer.
"""

from __future__ import annotations

import time
from datetime import datetime

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.logging import get_logger
from ..domain.base import DomainModel
from ..domain.enums import ObjectType, RetrieverKind
from ..domain.ports import EmbeddingProvider
from ..domain.retrieval import RetrievalConfig, SearchQuery
from .boosts import rank_candidates
from .candidates import DEFAULT_EF_SEARCH, keyword_candidates, semantic_candidates
from .config import load_retrieval_config
from .filters import ScopeFilters
from .fusion import merge_metadata, reciprocal_rank_fusion
from .types import HitKey, HitMetadata, RankedCandidate, RetrieverOutput, ScoredCandidate

__all__ = [
    "EMBEDDING_DEGRADED_WARNING",
    "HybridRetriever",
    "RetrievalOutcome",
    "resolve_embedding_model_id",
]

logger = get_logger(__name__)

#: Exact wording of the degraded-mode notices, so tests and the Ops page match on a constant.
EMBEDDING_DEGRADED_WARNING = "semantic retrieval unavailable; keyword-only results"
EMBEDDING_MODEL_UNKNOWN_WARNING = (
    "no embedding model is registered for this corpus; keyword-only results"
)


class RetrievalOutcome(DomainModel):
    """What P9 hands to P10: ranked candidates plus everything ``retrieval_logs`` needs.

    ``candidate_counts`` is per retriever (``semantic``, ``keyword``, ``fused``) and is MEASURED per
    query - it is the number the Ops page shows and the gold-set report compares.
    """

    hits: list[RankedCandidate] = Field(default_factory=list)
    fused: list[ScoredCandidate] = Field(default_factory=list)
    metadata: dict[str, HitMetadata] = Field(default_factory=dict)
    candidate_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    latency_ms: int = Field(default=0, ge=0)
    embedding_model_id: str | None = None
    config_version: str = "0.1.0"
    degraded: bool = False


def resolve_embedding_model_id(session: Session, embedder: EmbeddingProvider) -> str | None:
    """``embedding_models.id`` for the live provider - read-only, never registers a row.

    Lookup is by model *name*, matching :func:`aimemory.persistence.ingest_repo.ensure_embedding_model`
    (migration 0001 seeds the slug ``minilm-l6-v2-384`` while the provider reports the full
    ``sentence-transformers/...`` name). Returns ``None`` when the corpus has no embeddings from this
    model - the caller degrades to keyword-only rather than silently comparing vector spaces.
    """
    identity = embedder.model_identity()
    row = session.execute(
        text("SELECT id FROM embedding_models WHERE name = :name ORDER BY created_at LIMIT 1"),
        {"name": identity.name},
    ).first()
    return str(row[0]) if row is not None else None


class HybridRetriever:
    """Semantic + keyword candidates, RRF fusion and boosted ranking for one query.

    The session is passed per call (the repository convention: the caller owns the transaction) so
    the Gateway can run a search inside the same read transaction that later resolves provenance.
    """

    def __init__(
        self,
        embedder: EmbeddingProvider | None = None,
        *,
        config: RetrievalConfig | None = None,
        ef_search: int = DEFAULT_EF_SEARCH,
    ) -> None:
        self._embedder = embedder
        self._config = config or load_retrieval_config()
        self._ef_search = ef_search

    @property
    def config(self) -> RetrievalConfig:
        return self._config

    def retrieve(
        self,
        session: Session,
        query: SearchQuery,
        *,
        entity_linked_keys: set[HitKey] | None = None,
        now: datetime | None = None,
    ) -> RetrievalOutcome:
        """Run stages 1-3 and 6. ``entity_linked_keys`` is P10's graph-expansion feedback."""
        started = time.perf_counter()
        warnings: list[str] = []
        scope = ScopeFilters.from_query(query)
        object_types = list(query.object_types) or None

        semantic: RetrieverOutput | None = None
        model_id: str | None = None
        if self._embedder is not None:
            model_id = self._safe_model_id(session, warnings)
        if self._embedder is not None and model_id is not None:
            semantic = self._safe_semantic(session, query, scope, object_types, model_id, warnings)
        elif self._embedder is None:
            warnings.append(EMBEDDING_DEGRADED_WARNING)

        keyword = keyword_candidates(
            session,
            query.query,
            scope=scope,
            limit=self._config.keyword_top_k,
            object_types=object_types,
        )

        outputs = [output for output in (semantic, keyword) if output is not None]
        fused = reciprocal_rank_fusion(
            [output.candidates for output in outputs],
            rrf_k=self._config.rrf_k,
            limit=self._config.fused_top_k,
        )
        metadata = merge_metadata(outputs)
        hits = rank_candidates(
            fused,
            metadata,
            config=self._config,
            project_ids=query.project_ids,
            entity_linked_keys=entity_linked_keys or set(),
            limit=min(query.limit, self._config.final_k) if query.limit else self._config.final_k,
            now=now,
        )

        counts = {
            RetrieverKind.SEMANTIC.value: len(semantic.candidates) if semantic else 0,
            RetrieverKind.KEYWORD.value: len(keyword.candidates),
            "fused": len(fused),
            "returned": len(hits),
        }
        latency_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "retrieval.candidates",
            candidate_counts=counts,
            latency_ms=latency_ms,
            degraded=bool(warnings),
            rrf_k=self._config.rrf_k,
        )
        return RetrievalOutcome(
            hits=hits,
            fused=fused,
            metadata=metadata,
            candidate_counts=counts,
            warnings=warnings,
            latency_ms=latency_ms,
            embedding_model_id=model_id,
            config_version=self._config.version,
            degraded=bool(warnings),
        )

    # ------------------------------------------------------------------------------- degradation

    def _safe_model_id(self, session: Session, warnings: list[str]) -> str | None:
        assert self._embedder is not None
        try:
            model_id = resolve_embedding_model_id(session, self._embedder)
        except Exception as exc:  # noqa: BLE001 - any provider failure degrades, never fails
            logger.warning("retrieval.embedding_unavailable", error=type(exc).__name__)
            warnings.append(EMBEDDING_DEGRADED_WARNING)
            return None
        if model_id is None:
            logger.warning("retrieval.embedding_model_unregistered")
            warnings.append(EMBEDDING_MODEL_UNKNOWN_WARNING)
        return model_id

    def _safe_semantic(
        self,
        session: Session,
        query: SearchQuery,
        scope: ScopeFilters,
        object_types: list[ObjectType] | None,
        model_id: str,
        warnings: list[str],
    ) -> RetrieverOutput | None:
        assert self._embedder is not None
        try:
            vector = self._embedder.embed_query(query.query)
        except Exception as exc:  # noqa: BLE001 - embedding-service down -> keyword-only
            logger.warning("retrieval.embed_query_failed", error=type(exc).__name__)
            warnings.append(EMBEDDING_DEGRADED_WARNING)
            return None
        return semantic_candidates(
            session,
            vector,
            model_id=model_id,
            scope=scope,
            limit=self._config.semantic_top_k,
            object_types=object_types,
            ef_search=self._ef_search,
        )
