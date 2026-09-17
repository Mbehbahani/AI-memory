"""Stage 7: provenance attachment and citation rendering (``retrieval.md`` §7, plan section J).

The acceptance criterion this module exists for is absolute: **provenance completeness is 100 % on
returned evidence**. A hit whose ``[PROV]`` stamp cannot be resolved is *dropped* and a warning is
added - the system never cites something it cannot trace back to a source version.

Two shapes of hit, two sources of truth:

* **artifacts** come from ``provenance_v``, the view A04 built for exactly this (one query,
  ``object_id = ANY(:ids)``);
* **chunks** are not in ``provenance_v`` (it covers ``facts``/``knowledge_artifacts``/
  ``entity_mentions``), so their stamp is assembled from the rows a chunk *is* derived from:
  ``chunks -> source_versions -> sources -> source_roots``, plus the embedding model that indexed it
  and the episode that was created from its version. That is the same fourteen columns, read from
  the same chain the ingestion pipeline wrote.

Nothing here returns file bytes. ``ScoredHit.text`` is the stored chunk/artifact text that was
already retrieved; the *path* on disk never leaves the process (plan section Q).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.hashing import short_hash
from ..common.logging import get_logger
from ..domain.enums import ObjectType
from ..domain.provenance import Provenance
from ..domain.retrieval import ScoredHit
from .types import HitKey, RankedCandidate

__all__ = [
    "DEFAULT_CITATION_FORMAT",
    "PROVENANCE_MISSING_WARNING",
    "attach_provenance",
    "load_provenance",
    "render_citation",
]

logger = get_logger(__name__)

#: ``config/retrieval.yaml: context.citation_format``, repeated here as the fallback when the config
#: string is malformed - a query must not fail because a format placeholder was renamed.
DEFAULT_CITATION_FORMAT = "[{source_uri}#{heading} @{hash8}]"

PROVENANCE_MISSING_WARNING = "some hits were dropped: provenance could not be resolved"

_ARTIFACT_PROV_SQL = """
    SELECT p.object_id, p.source_id, p.source_uri, p.source_hash, p.source_version, p.project_id,
           p.device_id, p.observed_at, p.valid_from, p.valid_to, p.confidence,
           p.extraction_model_id, p.embedding_model_id, p.ingestion_run_id, p.episode_id
      FROM provenance_v p
     WHERE p.object_type = 'artifact'
       AND p.object_id = ANY(CAST(:ids AS uuid[]))
"""

_CHUNK_PROV_SQL = """
    SELECT c.id            AS object_id,
           c.source_id     AS source_id,
           s.uri           AS source_uri,
           sv.content_hash AS source_hash,
           c.version_id    AS source_version,
           c.project_id    AS project_id,
           COALESCE(sr.device_id, :device_id) AS device_id,
           sv.observed_at  AS observed_at,
           sv.ingestion_run_id AS ingestion_run_id,
           c.heading_path  AS heading_path,
           c.char_start    AS char_start,
           c.char_end      AS char_end,
           emb.model_id    AS embedding_model_id,
           ep.id           AS episode_id
      FROM chunks c
      JOIN source_versions sv ON sv.id = c.version_id
      JOIN sources s          ON s.id = c.source_id
      LEFT JOIN source_roots sr ON sr.root_id = s.root_id
      LEFT JOIN LATERAL (
           SELECT e.model_id FROM embeddings e
            WHERE e.text_hash = c.text_hash
            ORDER BY e.created_at DESC LIMIT 1
      ) emb ON true
      LEFT JOIN LATERAL (
           SELECT e2.id FROM episodes e2
            WHERE e2.version_id = c.version_id
            ORDER BY e2.created_at ASC LIMIT 1
      ) ep ON true
     WHERE c.id = ANY(CAST(:ids AS uuid[]))
"""


def render_citation(provenance: Provenance, template: str = DEFAULT_CITATION_FORMAT) -> str:
    """Render ``context.citation_format`` for one stamp.

    Placeholders: ``{source_uri}``, ``{heading}`` (the heading path joined with " > ") and
    ``{hash8}`` (:func:`aimemory.common.hashing.short_hash` of the source hash). An unknown
    placeholder falls back to the default format rather than raising - a config typo must not turn
    every search into a 500.
    """
    values = {
        "source_uri": provenance.source_uri or "unknown",
        "heading": " > ".join(provenance.heading_path) if provenance.heading_path else "",
        "hash8": short_hash(provenance.source_hash) if provenance.source_hash else "unknown",
    }
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        logger.warning("retrieval.citation_format_invalid", template=template)
        return DEFAULT_CITATION_FORMAT.format(**values)


def _provenance_from_row(row: dict[str, Any]) -> Provenance:
    return Provenance(
        source_id=row.get("source_id"),
        source_uri=row.get("source_uri"),
        source_hash=row.get("source_hash"),
        source_version=row.get("source_version"),
        project_id=row.get("project_id"),
        device_id=str(row.get("device_id") or "unknown"),
        observed_at=row["observed_at"],
        valid_from=row.get("valid_from"),
        valid_to=row.get("valid_to"),
        confidence=float(row["confidence"]) if row.get("confidence") is not None else 1.0,
        extraction_model_id=row.get("extraction_model_id"),
        embedding_model_id=row.get("embedding_model_id"),
        ingestion_run_id=row.get("ingestion_run_id"),
        episode_id=row.get("episode_id"),
        heading_path=list(row.get("heading_path") or []),
        char_start=row.get("char_start"),
        char_end=row.get("char_end"),
    )


def load_provenance(
    session: Session,
    keys: Iterable[HitKey],
    *,
    device_id: str = "unknown",
) -> dict[HitKey, Provenance]:
    """Resolve the ``[PROV]`` stamp of every key, in two queries (one per object type).

    ``device_id`` is only a *fallback* for a chunk whose source root row is missing; when the root is
    registered its ``device_id`` wins, because that is where the file actually lives (ADR-0004).
    """
    by_type: dict[ObjectType, list[str]] = {}
    for object_type, object_id in keys:
        by_type.setdefault(object_type, []).append(str(object_id))

    out: dict[HitKey, Provenance] = {}
    for object_type, sql, params in (
        (ObjectType.ARTIFACT, _ARTIFACT_PROV_SQL, {}),
        (ObjectType.CHUNK, _CHUNK_PROV_SQL, {"device_id": device_id}),
    ):
        ids = by_type.get(object_type)
        if not ids:
            continue
        rows = session.execute(text(sql), {"ids": ids, **params}).mappings()
        for row in rows:
            mapping = dict(row)
            key = (object_type, UUID(str(mapping["object_id"])))
            out[key] = _provenance_from_row(mapping)
    return out


def attach_provenance(
    session: Session,
    hits: Sequence[RankedCandidate],
    *,
    citation_format: str = DEFAULT_CITATION_FORMAT,
    entity_ids_by_hit: Mapping[HitKey, Sequence[UUID]] | None = None,
    device_id: str = "unknown",
) -> tuple[list[ScoredHit], list[str]]:
    """Promote ranked candidates to :class:`ScoredHit`, or drop them. Returns ``(hits, warnings)``.

    This is the single conversion point (:meth:`RankedCandidate.to_scored_hit`): every returned hit
    therefore has a populated ``provenance`` and a rendered ``citation`` by construction, and the
    ranks are renumbered after any drop so ``rank`` is always ``1..n`` with no holes.
    """
    if not hits:
        return [], []
    keys = [(hit.object_type, hit.object_id) for hit in hits]
    stamps = load_provenance(session, keys, device_id=device_id)
    entity_map = entity_ids_by_hit or {}

    scored: list[ScoredHit] = []
    dropped: list[HitKey] = []
    for hit in hits:
        key = (hit.object_type, hit.object_id)
        provenance = stamps.get(key)
        if provenance is None:
            dropped.append(key)
            continue
        entity_ids = list(entity_map.get(key, ()))
        if entity_ids:
            hit.metadata.entity_ids = entity_ids
        scored.append(
            hit.to_scored_hit(provenance, citation=render_citation(provenance, citation_format))
        )
    for position, item in enumerate(scored, start=1):
        item.rank = position

    warnings: list[str] = []
    if dropped:
        logger.warning(
            "retrieval.provenance_unresolved",
            dropped=len(dropped),
            object_types=sorted({key[0].value for key in dropped}),
        )
        warnings.append(PROVENANCE_MISSING_WARNING)
    return scored, warnings
