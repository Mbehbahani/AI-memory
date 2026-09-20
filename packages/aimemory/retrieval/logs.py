"""Stage 9: the ``retrieval_logs`` row (``retrieval.md`` §9, plan section P "every query logged").

One row per Gateway query: the text and its hash, the scoping, ``as_of``/``since``, the *effective*
tuning parameters, the per-retriever candidate counts, the returned object ids, the latency, the
client and the warnings. ADR-0010's gold-set comparison across model and config changes is only
possible because this row exists - a ranking regression has to be attributable without re-running
the query against a database that has since changed.

Two rules:

* **A logging failure never fails a query.** :func:`write_retrieval_log` catches everything and
  returns ``None``; the caller has already produced a correct answer and the log is an observation
  of it, not part of it.
* **Only the query text is stored, never file content.** ``result_ids`` are ids; the hit text stays
  in ``chunks``/``knowledge_artifacts`` where it already lives (plan section T).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from ..common.hashing import text_hash
from ..common.logging import get_logger
from ..common.time import utc_now
from ..domain.models import RetrievalLog
from ..domain.retrieval import RetrievalConfig, ScoredHit, SearchQuery
from ..persistence.repositories import AuditRepo
from .relevance import W_LEXICAL, W_SEMANTIC

__all__ = ["effective_params", "log_payload", "write_retrieval_log"]

logger = get_logger(__name__)


def effective_params(config: RetrievalConfig) -> dict[str, float]:
    """The weights this query actually ran with - what makes a stored row replayable.

    ``retrieval_logs.params`` is typed ``dict[str, float]`` by the frozen contract, so the config
    *version* (a string) cannot travel in it; it goes to the structured log line instead.
    """
    return {
        "semantic_top_k": float(config.semantic_top_k),
        "keyword_top_k": float(config.keyword_top_k),
        "fused_top_k": float(config.fused_top_k),
        "final_k": float(config.final_k),
        "rrf_k": float(config.rrf_k),
        "project_match": float(config.boost_project_match),
        "entity_linked": float(config.boost_entity_linked),
        "unconfirmed_penalty": float(config.unconfirmed_penalty),
        "low_trust_penalty": float(config.low_trust_penalty),
        "recency_half_life_days": float(config.recency_half_life_days),
        "graph_max_nodes": float(config.graph_max_nodes),
        "token_budget": float(config.token_budget),
        "w_semantic": float(W_SEMANTIC),
        "w_lexical": float(W_LEXICAL),
    }


def write_retrieval_log(
    session: Session,
    query: SearchQuery,
    *,
    hits: Sequence[ScoredHit],
    candidate_counts: Mapping[str, int],
    latency_ms: int,
    warnings: Sequence[str] = (),
    config: RetrievalConfig | None = None,
    client: str | None = None,
    config_version: str | None = None,
    embedding_model_id: str | None = None,
) -> UUID | None:
    """Insert one ``retrieval_logs`` row. Returns its id, or ``None`` if the insert failed.

    The row is written on the caller's session; the caller commits (memory-api commits the whole
    read transaction at the end of the request, so the log lands with the answer).
    """
    entry = RetrievalLog(
        id=uuid4(),
        at=utc_now(),
        query_text=query.query,
        query_hash=text_hash(query.query),
        project_ids=list(query.project_ids),
        object_types=list(query.object_types),
        as_of=query.as_of,
        since=query.since,
        limit=query.limit,
        expand=query.expand,
        params=effective_params(config) if config is not None else {},
        candidate_counts=dict(candidate_counts),
        result_ids=[hit.object_id for hit in hits],
        latency_ms=max(int(latency_ms), 0),
        client=client or query.client,
        warnings=list(warnings),
    )
    try:
        AuditRepo(session).log_retrieval(entry)
    except Exception as exc:  # noqa: BLE001 - an observation must never break the thing observed
        logger.warning("retrieval.log_write_failed", error=type(exc).__name__)
        return None
    logger.info(
        "retrieval.logged",
        retrieval_log_id=str(entry.id),
        results=len(entry.result_ids),
        latency_ms=entry.latency_ms,
        config_version=config_version,
        embedding_model_id=embedding_model_id,
        warnings=len(entry.warnings),
    )
    return entry.id


def log_payload(entry: RetrievalLog) -> dict[str, Any]:
    """Serialization used by the Ops page and the evaluation report."""
    return entry.model_dump(mode="json")
