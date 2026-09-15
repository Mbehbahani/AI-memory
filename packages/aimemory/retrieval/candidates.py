"""Stage 1 and 2 of the pipeline: semantic and keyword candidates (``retrieval.md`` §1-§2).

Both retrievers run two identically shaped queries - one over ``chunks``, one over
``knowledge_artifacts`` - merge them by score, and return the top ``k`` as
:class:`~aimemory.domain.retrieval.Candidate` objects *plus* the
:class:`~aimemory.retrieval.types.HitMetadata` the ranking, citation and provenance stages need, so
nothing downstream has to re-read the rows the filters were evaluated against.

Design notes that are load-bearing:

* Every scoping predicate is inside the SQL (:mod:`aimemory.retrieval.filters`).
* ``SET LOCAL hnsw.ef_search`` is issued per statement - at this corpus size recall matters more
  than the few milliseconds it costs (``retrieval.md`` deviation 6).
* The query vector is bound as a text literal and cast (``CAST(:query_vector AS vector)``) rather
  than relying on a psycopg adapter: ``pgvector.psycopg.register_vector`` registers dumpers for
  ``Vector`` and ``numpy.ndarray`` only, so a bare ``list[float]`` reaches the server as
  ``float8[]``. That casts fine on INSERT (pgvector ships an assignment cast) but **not** as an
  operand of ``<=>``, which is exactly what this module needs.
* Keyword search is never skipped: it is what makes ``ADR-0007`` or ``joblab-de`` findable when the
  embedding is too diffuse.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..domain.enums import ObjectType, RetrieverKind
from ..domain.retrieval import Candidate
from .filters import ARTIFACT_PREDICATES, CHUNK_PREDICATES, ScopeFilters
from .types import HitMetadata, RetrieverOutput

__all__ = [
    "DEFAULT_EF_SEARCH",
    "keyword_candidates",
    "semantic_candidates",
    "set_ef_search",
    "vector_literal",
]

#: ``hnsw.ef_search`` for the candidate statement. Not in the plan; see ``retrieval.md`` deviation 6.
DEFAULT_EF_SEARCH = 80

_CHUNK_COLUMNS = """
               c.id           AS object_id,
               c.project_id   AS project_id,
               c.source_id    AS source_id,
               c.version_id   AS version_id,
               c.text         AS text,
               c.text_hash    AS text_hash,
               c.heading_path AS heading_path,
               s.uri          AS source_uri,
               s.status       AS source_status,
               s.trust        AS trust,
               s.policy       AS policy,
               sv.observed_at AS observed_at
"""

_ARTIFACT_COLUMNS = """
               a.id             AS object_id,
               a.project_id     AS project_id,
               a.source_id      AS source_id,
               a.source_version AS version_id,
               a.title          AS title,
               a.body           AS text,
               a.source_hash    AS text_hash,
               a.current_status AS status,
               a.source_status  AS source_status,
               a.valid_from     AS valid_from,
               a.valid_to       AS valid_to,
               a.observed_at    AS observed_at,
               s.policy         AS policy,
               COALESCE(s.uri, a.source_uri) AS source_uri,
               COALESCE(s.trust, 'high')     AS trust
"""

_SEMANTIC_CHUNKS_SQL = f"""
        SELECT {_CHUNK_COLUMNS},
               1 - (e.vector <=> CAST(:query_vector AS vector)) AS raw_score
          FROM embeddings e
          JOIN chunks c           ON c.id = e.object_id
          JOIN sources s          ON s.id = c.source_id
          JOIN source_versions sv ON sv.id = c.version_id
         WHERE e.object_type = 'chunk'
           AND e.model_id = :model_id
           {CHUNK_PREDICATES}
         ORDER BY e.vector <=> CAST(:query_vector AS vector)
         LIMIT :k
"""

_SEMANTIC_ARTIFACTS_SQL = f"""
        SELECT {_ARTIFACT_COLUMNS},
               1 - (e.vector <=> CAST(:query_vector AS vector)) AS raw_score
          FROM embeddings e
          JOIN knowledge_artifacts a ON a.id = e.object_id
          LEFT JOIN sources s        ON s.id = a.source_id
         WHERE e.object_type = 'artifact'
           AND e.model_id = :model_id
           {ARTIFACT_PREDICATES}
         ORDER BY e.vector <=> CAST(:query_vector AS vector)
         LIMIT :k
"""

_KEYWORD_CHUNKS_SQL = f"""
        SELECT {_CHUNK_COLUMNS},
               ts_rank_cd(c.tsv, q.q) AS raw_score
          FROM plainto_tsquery('english', :query_text) AS q(q)
          JOIN chunks c           ON c.tsv @@ q.q
          JOIN sources s          ON s.id = c.source_id
          JOIN source_versions sv ON sv.id = c.version_id
         WHERE true
           {CHUNK_PREDICATES}
         ORDER BY raw_score DESC, c.id
         LIMIT :k
"""

# The tsvector expression must match ``ix_artifacts_tsv`` character for character to stay
# index-backed (migration 0001: ``to_tsvector('english', title || ' ' || body)``).
_KEYWORD_ARTIFACTS_SQL = f"""
        SELECT {_ARTIFACT_COLUMNS},
               ts_rank_cd(to_tsvector('english', a.title || ' ' || a.body), q.q) AS raw_score
          FROM plainto_tsquery('english', :query_text) AS q(q)
          JOIN knowledge_artifacts a
            ON to_tsvector('english', a.title || ' ' || a.body) @@ q.q
          LEFT JOIN sources s ON s.id = a.source_id
         WHERE true
           {ARTIFACT_PREDICATES}
         ORDER BY raw_score DESC, a.id
         LIMIT :k
"""


def vector_literal(vector: Sequence[float]) -> str:
    """``[0.1,-0.2,...]`` - pgvector's text input format, round-trippable via ``repr(float)``."""
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def set_ef_search(session: Session, ef_search: int = DEFAULT_EF_SEARCH) -> None:
    """``SET LOCAL hnsw.ef_search`` for the current transaction (recall over latency)."""
    session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))


def _wants(object_types: Sequence[ObjectType] | None, kind: ObjectType) -> bool:
    """Empty ``object_types`` means chunks + artifacts (``SearchQuery.object_types``)."""
    return not object_types or kind in object_types


def _metadata_from_row(row: dict[str, Any], object_type: ObjectType) -> HitMetadata:
    return HitMetadata(
        object_type=object_type,
        object_id=row["object_id"],
        project_id=row.get("project_id"),
        source_id=row.get("source_id"),
        source_uri=row.get("source_uri"),
        version_id=row.get("version_id"),
        title=row.get("title"),
        text=row.get("text") or "",
        text_hash=row.get("text_hash"),
        heading_path=list(row.get("heading_path") or []),
        observed_at=row.get("observed_at"),
        status=row.get("status"),
        source_status=row.get("source_status") or "active",
        trust=row.get("trust") or "high",
        policy=row.get("policy"),
        valid_from=row.get("valid_from"),
        valid_to=row.get("valid_to"),
    )


_Collected = list[tuple[float, ObjectType, UUID, HitMetadata]]


def _collect(session: Session, sql: str, params: dict[str, Any], kind: ObjectType) -> _Collected:
    rows = session.execute(text(sql), params).mappings().all()
    out: _Collected = []
    for row in rows:
        mapping = dict(row)
        out.append(
            (
                float(mapping["raw_score"]),
                kind,
                mapping["object_id"],
                _metadata_from_row(mapping, kind),
            )
        )
    return out


def _to_output(retriever: RetrieverKind, collected: _Collected, limit: int) -> RetrieverOutput:
    """Merge the chunk and artifact lists, rank them 1..k and build the retriever's output.

    Ties break on ``object_id`` so a retriever's own ordering is deterministic before fusion ever
    sees it (``retrieval.md`` deviation 3 requires reproducible gold-set numbers).
    """
    collected.sort(key=lambda item: (-item[0], str(item[2])))
    candidates: list[Candidate] = []
    metadata: dict[str, HitMetadata] = {}
    for index, (score, object_type, object_id, meta) in enumerate(collected[:limit], start=1):
        candidates.append(
            Candidate(
                object_type=object_type,
                object_id=object_id,
                retriever=retriever,
                rank=index,
                raw_score=score,
                project_id=meta.project_id,
                source_id=meta.source_id,
                text=meta.text,
            )
        )
        metadata[f"{object_type.value}:{object_id}"] = meta
    return RetrieverOutput(retriever=retriever, candidates=candidates, metadata=metadata)


def semantic_candidates(
    session: Session,
    query_vector: Sequence[float],
    *,
    model_id: str,
    scope: ScopeFilters,
    limit: int,
    object_types: Sequence[ObjectType] | None = None,
    ef_search: int = DEFAULT_EF_SEARCH,
) -> RetrieverOutput:
    """pgvector HNSW cosine top-``limit`` over chunks and artifacts for one query embedding.

    ``model_id`` must be the corpus' embedding model (the ``(text_hash, model_id)`` reuse key of
    ``embeddings``); mixing models would compare vectors from two different spaces.
    """
    set_ef_search(session, ef_search)
    params = scope.bind_params() | {
        "query_vector": vector_literal(query_vector),
        "model_id": model_id,
        "k": limit,
    }
    collected: _Collected = []
    if _wants(object_types, ObjectType.CHUNK):
        collected += _collect(session, _SEMANTIC_CHUNKS_SQL, params, ObjectType.CHUNK)
    if _wants(object_types, ObjectType.ARTIFACT):
        collected += _collect(session, _SEMANTIC_ARTIFACTS_SQL, params, ObjectType.ARTIFACT)
    return _to_output(RetrieverKind.SEMANTIC, collected, limit)


def keyword_candidates(
    session: Session,
    query_text: str,
    *,
    scope: ScopeFilters,
    limit: int,
    object_types: Sequence[ObjectType] | None = None,
) -> RetrieverOutput:
    """Postgres full-text top-``limit`` (``ts_rank_cd``) over chunks and artifacts.

    A query that ``plainto_tsquery`` reduces to nothing (only stop words, or punctuation) yields an
    empty list rather than an error - fusion then runs on the semantic list alone.
    """
    params = scope.bind_params() | {"query_text": query_text, "k": limit}
    collected: _Collected = []
    if _wants(object_types, ObjectType.CHUNK):
        collected += _collect(session, _KEYWORD_CHUNKS_SQL, params, ObjectType.CHUNK)
    if _wants(object_types, ObjectType.ARTIFACT):
        collected += _collect(session, _KEYWORD_ARTIFACTS_SQL, params, ObjectType.ARTIFACT)
    return _to_output(RetrieverKind.KEYWORD, collected, limit)
