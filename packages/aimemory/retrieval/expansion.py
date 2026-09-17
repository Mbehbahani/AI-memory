"""Stage 4: graph expansion (``retrieval.md`` §4, plan section P step 3).

Two steps, in this order, because the second one must never be able to fail the query:

1. **Seeds, from PostgreSQL.** The entity ids of the fused hits - for a chunk through
   ``entity_mentions.chunk_id``, for an artifact through ``artifact_entities``. This is also what
   fills :attr:`~aimemory.domain.retrieval.ScoredHit.entity_ids`, so a hit knows which entities it
   talks about even when the graph is down.
2. **Neighbours, from Neo4j.** One bounded Cypher call (``max_depth`` 1, ``max_nodes`` 25,
   restricted to ``graph_expansion.relationship_types``) with the ADR-0005 point-in-time predicate
   applied to the *edge*. The neighbours become
   :class:`~aimemory.domain.retrieval.RelatedEntity` objects, and every fused hit that mentions one
   of them earns the ``entity_linked`` boost through ``HybridRetriever.retrieve(...,
   entity_linked_keys=...)``.

**Degradation is mandatory** (plan section Y failure test "Neo4j down -> vector-only with warning"):
every Neo4j failure is caught here, logged, and turned into :data:`GRAPH_DEGRADED_WARNING` in
:attr:`GraphExpansion.warnings`. The seeds from step 1 survive, so a degraded query still returns
hits, entity ids and provenance - only the neighbours and the ``entity_linked`` boost are missing.

Two deliberate departures from the literal Cypher in ``retrieval.md`` §4, both forced by what the
projection actually writes (:mod:`aimemory.providers.graph.projection` and
``Neo4jGraphStore.upsert_relationships``):

* ``r.valid_from`` is stored as an **ISO string** (the store converts every datetime property before
  the MERGE) while ``invalidate_relationship`` writes ``r.valid_to`` as a real Neo4j ``DateTime``.
  ``datetime(toString(x))`` normalises both, so the temporal predicate holds for either
  representation instead of silently evaluating to null on a type mismatch.
* The neighbour's entity type is read from the ``type`` property (which keeps ``SubProject``) and
  falls back to the node's ontology label, because the *stored label* of a ``SubProject`` is
  ``Project`` (ontology.md §5).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.logging import get_logger
from ..common.time import ensure_utc, parse_timestamp, utc_now
from ..domain.enums import EntityType, ObjectType
from ..domain.ports import GraphStore
from ..domain.retrieval import RelatedEntity, RetrievalConfig
from .types import HitKey

__all__ = [
    "GRAPH_DEGRADED_WARNING",
    "GraphExpansion",
    "expand",
    "neighbours",
    "seed_entity_ids",
]

logger = get_logger(__name__)

#: Exact wording required by ``retrieval.md`` §4; tests and the Ops page match on this constant.
GRAPH_DEGRADED_WARNING = "graph expansion unavailable; vector+keyword only"

#: Neighbours of the seed entities, one hop, bounded and temporally filtered on the edge.
NEIGHBOUR_CYPHER = """
MATCH (n:Entity)-[r]-(m:Entity)
WHERE n.id IN $entity_ids
  AND m.id <> n.id
  AND type(r) IN $relationship_types
  AND (r.valid_from IS NULL OR datetime(toString(r.valid_from)) <= datetime($as_of))
  AND (r.valid_to   IS NULL OR datetime(toString(r.valid_to))   >  datetime($as_of))
  AND ($project_ids IS NULL OR m.project_id IN $project_ids)
RETURN DISTINCT m.id AS id, m.name AS name, m.type AS type, labels(m) AS labels,
       m.project_id AS project_id, type(r) AS predicate, r.fact_id AS fact_id,
       r.confidence AS confidence, r.valid_from AS valid_from, r.valid_to AS valid_to,
       startNode(r).id = n.id AS outgoing
LIMIT $max_nodes
"""

_CHUNK_ENTITIES_SQL = """
    SELECT em.chunk_id AS object_id, em.entity_id AS entity_id
      FROM entity_mentions em
     WHERE em.chunk_id = ANY(CAST(:ids AS uuid[]))
"""

_ARTIFACT_ENTITIES_SQL = """
    SELECT ae.artifact_id AS object_id, ae.entity_id AS entity_id
      FROM artifact_entities ae
     WHERE ae.artifact_id = ANY(CAST(:ids AS uuid[]))
"""


@dataclass(frozen=True, slots=True)
class GraphExpansion:
    """What stage 4 hands back. Never raises; a graph failure arrives as a warning."""

    related: list[RelatedEntity] = field(default_factory=list)
    entity_ids_by_hit: dict[HitKey, list[UUID]] = field(default_factory=dict)
    entity_linked_keys: set[HitKey] = field(default_factory=set)
    seed_entity_ids: list[UUID] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False

    @property
    def counts(self) -> dict[str, int]:
        """MEASURED per query; merged into ``retrieval_logs.candidate_counts``."""
        return {
            "graph_seeds": len(self.seed_entity_ids),
            "graph_related": len(self.related),
            "graph_linked_hits": len(self.entity_linked_keys),
        }


def seed_entity_ids(session: Session, keys: Iterable[HitKey]) -> dict[HitKey, list[UUID]]:
    """``{hit -> entity ids it mentions}`` for the fused hits, from PostgreSQL only.

    Chunks resolve through ``entity_mentions.chunk_id`` and artifacts through ``artifact_entities``
    (``retrieval.md`` §4 step 1). Ordering is stable (sorted) so two identical queries produce the
    identical ``ScoredHit.entity_ids`` and a gold-set diff stays meaningful.
    """
    wanted = list(keys)
    by_type: dict[ObjectType, list[str]] = {}
    for object_type, object_id in wanted:
        by_type.setdefault(object_type, []).append(str(object_id))

    out: dict[HitKey, set[UUID]] = {key: set() for key in wanted}
    for object_type, sql in (
        (ObjectType.CHUNK, _CHUNK_ENTITIES_SQL),
        (ObjectType.ARTIFACT, _ARTIFACT_ENTITIES_SQL),
    ):
        ids = by_type.get(object_type)
        if not ids:
            continue
        for row in session.execute(text(sql), {"ids": ids}).mappings():
            key = (object_type, UUID(str(row["object_id"])))
            out.setdefault(key, set()).add(UUID(str(row["entity_id"])))
    return {key: sorted(values, key=str) for key, values in out.items()}


def _as_datetime(value: Any) -> datetime | None:
    """Neo4j ``DateTime``, ISO string or ``None`` -> an aware ``datetime`` (or ``None``)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        native = to_native()
        return ensure_utc(native) if isinstance(native, datetime) else None
    return parse_timestamp(str(value))


def _entity_type(row: dict[str, Any]) -> EntityType | None:
    """``m.type`` first (it keeps ``SubProject``), then the first ontology label on the node."""
    raw = row.get("type")
    if raw:
        try:
            return EntityType(str(raw))
        except ValueError:
            logger.warning("retrieval.expansion_unknown_type", value=str(raw))
    for label in row.get("labels") or []:
        if label == "Entity":
            continue
        try:
            return EntityType(str(label))
        except ValueError:
            continue
    return None


def _to_related(row: dict[str, Any]) -> RelatedEntity | None:
    """One Cypher row -> :class:`RelatedEntity`, or ``None`` when it is not a citable entity."""
    identifier, name = row.get("id"), row.get("name")
    entity_type = _entity_type(row)
    if not identifier or not name or entity_type is None:
        return None
    try:
        entity_id = UUID(str(identifier))
    except ValueError:  # a registry node with a slug id is not an entity we can cite
        return None
    confidence = row.get("confidence")
    return RelatedEntity(
        entity_id=entity_id,
        name=str(name),
        type=entity_type,
        predicate=str(row.get("predicate") or ""),
        direction="out" if row.get("outgoing") else "in",
        project_id=row.get("project_id"),
        valid_from=_as_datetime(row.get("valid_from")),
        valid_to=_as_datetime(row.get("valid_to")),
        confidence=min(max(float(confidence), 0.0), 1.0) if confidence is not None else 1.0,
    )


def neighbours(
    graph: GraphStore,
    entity_ids: Sequence[UUID],
    *,
    config: RetrievalConfig,
    as_of: datetime | None = None,
    project_ids: Sequence[str] | None = None,
) -> list[RelatedEntity]:
    """One bounded Cypher call. Raises on a graph failure - :func:`expand` is what degrades."""
    if not entity_ids:
        return []
    moment = ensure_utc(as_of) if as_of is not None else utc_now()
    rows = graph.query(
        NEIGHBOUR_CYPHER,
        {
            "entity_ids": [str(eid) for eid in entity_ids],
            "relationship_types": list(config.graph_relationship_types),
            "as_of": moment.isoformat(),
            "project_ids": list(project_ids) if project_ids else None,
            "max_nodes": config.graph_max_nodes,
        },
    )
    related: list[RelatedEntity] = []
    seen: set[tuple[UUID, str, str]] = set()
    for row in rows:
        item = _to_related(dict(row))
        if item is None:
            continue
        identity = (item.entity_id, item.predicate, item.direction)
        if identity in seen:
            continue
        seen.add(identity)
        related.append(item)
    return related[: config.graph_max_nodes]


def expand(
    session: Session,
    graph: GraphStore | None,
    keys: Iterable[HitKey],
    *,
    config: RetrievalConfig,
    as_of: datetime | None = None,
    project_ids: Sequence[str] | None = None,
    enabled: bool = True,
) -> GraphExpansion:
    """Stage 4 end to end. Returns an empty-but-valid expansion instead of raising, always.

    ``enabled`` is ``SearchQuery.expand and config.graph_expansion_enabled``; when it is false the
    seeds are still resolved (a hit's ``entity_ids`` are part of the contract) but Neo4j is not
    consulted and no warning is emitted - that is a requested mode, not a degradation.
    """
    hit_entities = seed_entity_ids(session, keys)
    seeds = sorted({eid for ids in hit_entities.values() for eid in ids}, key=str)

    if not enabled or graph is None or not seeds:
        return GraphExpansion(entity_ids_by_hit=hit_entities, seed_entity_ids=seeds)

    try:
        related = neighbours(graph, seeds, config=config, as_of=as_of, project_ids=project_ids)
    except Exception as exc:  # noqa: BLE001 - ANY graph failure degrades; it never fails the query
        logger.warning("retrieval.graph_unavailable", error=type(exc).__name__)
        return GraphExpansion(
            entity_ids_by_hit=hit_entities,
            seed_entity_ids=seeds,
            warnings=[GRAPH_DEGRADED_WARNING],
            degraded=True,
        )

    neighbour_ids = {item.entity_id for item in related}
    linked = {key for key, ids in hit_entities.items() if neighbour_ids.intersection(ids)}
    logger.info(
        "retrieval.graph_expanded",
        seeds=len(seeds),
        related=len(related),
        linked_hits=len(linked),
    )
    return GraphExpansion(
        related=related,
        entity_ids_by_hit=hit_entities,
        entity_linked_keys=linked,
        seed_entity_ids=seeds,
    )
