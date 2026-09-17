"""Stage 5: the temporal filter (``retrieval.md`` §5, ADR-0005 §5).

The artifact half of this stage already runs *inside* the candidate SQL
(:data:`aimemory.retrieval.filters.ARTIFACT_PREDICATES`), which is where a filter belongs - filtering
after ``LIMIT`` silently empties a scoped query. What lives here is the other half:

* :func:`filter_hits` re-applies the point-in-time predicate to the ranked list in Python. It is
  defence in depth, not duplication: a hit can reach ranking through a path that did not evaluate the
  predicate (a future retriever, a cached candidate, a caller-supplied list), and returning an
  artifact that was already superseded at ``as_of`` would be a *wrong answer*, not a ranking nit.
* :func:`current_facts` is the fact side of the stage, which has no candidate SQL of its own: facts
  are not embedded or tsvector-indexed in V0.1 - they reach the caller through the entities their
  evidence mentions (``current_facts`` block of the assembled context, ``/v1/entities/{id}``).

``as_of`` defaults to now. ``status = 'unconfirmed'`` rows are **kept** (ADR-0005 rule 3) and
penalised in stage 6; ``include_unconfirmed=False`` drops them entirely for callers who want only
re-confirmed knowledge.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.time import ensure_utc, is_valid_at, utc_now
from ..domain.enums import FactStatus, ObjectType
from ..domain.provenance import Provenance
from .types import RankedCandidate

__all__ = [
    "TEMPORAL_DROP_WARNING",
    "FactRow",
    "current_facts",
    "facts_for_entities",
    "filter_hits",
]

#: Appended to ``SearchResult.warnings`` when the temporal filter removed a ranked hit, so a short
#: result set is explainable rather than mysterious.
TEMPORAL_DROP_WARNING = "some hits were outside the requested as_of window and were dropped"

_FACTS_SQL = """
    SELECT f.id, f.statement, f.predicate, f.status, f.confidence, f.project_id,
           f.subject_entity_id, f.object_entity_id, f.object_value,
           f.valid_from, f.valid_to, f.observed_at,
           subj.canonical_name AS subject_name, obj.canonical_name AS object_name,
           f.source_id, f.source_uri, f.source_hash, f.source_version, f.device_id,
           f.extraction_model_id, f.embedding_model_id, f.ingestion_run_id, f.episode_id
      FROM facts f
      JOIN entities subj ON subj.id = f.subject_entity_id
      LEFT JOIN entities obj ON obj.id = f.object_entity_id
     WHERE (
             CAST(:entity_ids AS uuid[]) IS NULL
             OR f.subject_entity_id = ANY(CAST(:entity_ids AS uuid[]))
             OR f.object_entity_id  = ANY(CAST(:entity_ids AS uuid[]))
           )
       AND (CAST(:project_ids AS text[]) IS NULL OR f.project_id = ANY(CAST(:project_ids AS text[])))
       AND f.valid_from <= CAST(:as_of AS timestamptz)
       AND (f.valid_to IS NULL OR f.valid_to > CAST(:as_of AS timestamptz))
       AND (CAST(:include_unconfirmed AS boolean) OR f.status <> 'unconfirmed')
       AND f.source_status = ANY(CAST(:allowed_source_status AS text[]))
     ORDER BY f.valid_from ASC, f.id
     LIMIT :limit
"""


@dataclass(frozen=True, slots=True)
class FactRow:
    """One current fact plus its ``[PROV]`` stamp - the unit of the ``current_facts`` block."""

    id: UUID
    statement: str
    predicate: str
    status: str
    confidence: float
    project_id: str | None
    subject_name: str
    object_name: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    observed_at: datetime | None
    provenance: Provenance

    @property
    def is_unconfirmed(self) -> bool:
        return self.status == FactStatus.UNCONFIRMED.value


def _fact_from_row(row: dict[str, Any]) -> FactRow:
    provenance = Provenance(
        source_id=row.get("source_id"),
        source_uri=row.get("source_uri"),
        source_hash=row.get("source_hash"),
        source_version=row.get("source_version"),
        project_id=row.get("project_id"),
        device_id=str(row.get("device_id") or "unknown"),
        observed_at=row["observed_at"],
        valid_from=row.get("valid_from"),
        valid_to=row.get("valid_to"),
        confidence=float(row.get("confidence") or 1.0),
        extraction_model_id=row.get("extraction_model_id"),
        embedding_model_id=row.get("embedding_model_id"),
        ingestion_run_id=row.get("ingestion_run_id"),
        episode_id=row.get("episode_id"),
    )
    return FactRow(
        id=UUID(str(row["id"])),
        statement=str(row["statement"]),
        predicate=str(row["predicate"]),
        status=str(row["status"]),
        confidence=float(row.get("confidence") or 1.0),
        project_id=row.get("project_id"),
        subject_name=str(row.get("subject_name") or ""),
        object_name=row.get("object_name") or row.get("object_value"),
        valid_from=row.get("valid_from"),
        valid_to=row.get("valid_to"),
        observed_at=row.get("observed_at"),
        provenance=provenance,
    )


def facts_for_entities(
    session: Session,
    entity_ids: Sequence[UUID] | None,
    *,
    as_of: datetime | None = None,
    project_ids: Sequence[str] | None = None,
    include_unconfirmed: bool = True,
    allowed_source_status: Sequence[str] = ("active",),
    limit: int = 50,
) -> list[FactRow]:
    """Facts valid at ``as_of`` whose subject or object is one of ``entity_ids``.

    ``entity_ids=None`` means "any entity" (used by ``/v1/state`` for a project-wide view); the
    project filter and the ``LIMIT`` keep that bounded. Ordered oldest-stable first, which is the
    order ``retrieval.md`` §8 prescribes for the ``current_facts`` block.
    """
    rows = session.execute(
        text(_FACTS_SQL),
        {
            "entity_ids": [str(e) for e in entity_ids] if entity_ids is not None else None,
            "project_ids": list(project_ids) if project_ids else None,
            "as_of": ensure_utc(as_of) if as_of is not None else utc_now(),
            "include_unconfirmed": include_unconfirmed,
            "allowed_source_status": list(allowed_source_status),
            "limit": limit,
        },
    ).mappings()
    return [_fact_from_row(dict(row)) for row in rows]


#: Alias kept because ``retrieval.md`` §8 names the block ``current_facts``.
current_facts = facts_for_entities


def filter_hits(
    hits: Iterable[RankedCandidate],
    *,
    as_of: datetime | None = None,
    include_unconfirmed: bool = True,
) -> tuple[list[RankedCandidate], list[RankedCandidate]]:
    """Split ranked hits into ``(kept, dropped)`` by the ADR-0005 predicate.

    Chunks are immutable text with no validity window of their own - they are scoped by their
    source's status instead (``retrieval.md`` §5) - so they are always kept here. Artifacts carry
    ``valid_from``/``valid_to`` and are evaluated; a ``None`` ``valid_from`` counts as "always
    started" (see :func:`aimemory.common.time.is_valid_at`) so rows written before a temporal engine
    ran are never silently lost.
    """
    moment = ensure_utc(as_of) if as_of is not None else utc_now()
    kept: list[RankedCandidate] = []
    dropped: list[RankedCandidate] = []
    for hit in hits:
        if hit.object_type is not ObjectType.ARTIFACT:
            kept.append(hit)
            continue
        meta = hit.metadata
        valid = is_valid_at(meta.valid_from, meta.valid_to, moment)
        confirmed = include_unconfirmed or meta.status != FactStatus.UNCONFIRMED.value
        (kept if valid and confirmed else dropped).append(hit)
    for position, hit in enumerate(kept, start=1):
        hit.rank = position
    return kept, dropped
