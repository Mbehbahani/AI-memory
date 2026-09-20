"""Applying a :class:`ProjectionPlan` to Neo4j, in the only order that works (A08).

``Neo4jGraphStore.upsert_relationships`` resolves each endpoint's **live** label to validate the edge,
so both endpoint nodes must exist before the edge is written. :meth:`GraphProjector.apply` therefore
does exactly three things, in this order:

1. fold literal-valued functional facts into their subject node's properties (``ontology.md`` §4 -
   they are node properties, not edges), then ``MERGE`` every node;
2. ``MERGE`` every relationship;
3. report what was dropped and why.

ADR-0001 is the reason this class swallows nothing and raises nothing by default: PostgreSQL is
already written when we get here. A Neo4j failure must be *reported* (the run records a
``project_graph`` job failure and ``rebuild-graph`` fixes it later), never allowed to roll back
knowledge that is safely persisted. Pass ``strict=True`` to get the exception instead - the rebuild
driver uses that, because there a failure means the rebuild did not happen.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ...common.logging import get_logger
from ...domain.ports import GraphNode, GraphStore
from ...ontology import Ontology, load_ontology
from .projection import ProjectionPlan

__all__ = [
    "GraphProjector",
    "ProjectionReport",
    "merge_nodes",
    "prune_dangling",
    "wipe_projection",
]

logger = get_logger(__name__)


@dataclass(slots=True)
class ProjectionReport:
    """MEASURED counts of one projection pass."""

    nodes_written: int = 0
    relationships_written: int = 0
    dropped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def counts(self) -> dict[str, int]:
        return {
            "nodes": self.nodes_written,
            "relationships": self.relationships_written,
            "dropped": len(self.dropped),
            "errors": len(self.errors),
        }


class GraphProjector:
    """Writes a plan. Owns no transaction: Neo4j writes are idempotent MERGEs keyed on Postgres ids."""

    def __init__(self, store: GraphStore, *, strict: bool = False) -> None:
        self._store = store
        self._strict = strict

    def apply(self, plan: ProjectionPlan) -> ProjectionReport:
        report = ProjectionReport(dropped=list(plan.dropped))
        nodes = _fold_property_updates(plan, report)

        try:
            report.nodes_written = self._store.upsert_nodes(nodes) if nodes else 0
        except Exception as exc:  # noqa: BLE001 - see the module docstring (ADR-0001)
            message = f"node projection failed: {type(exc).__name__}: {exc}"
            report.errors.append(message)
            logger.warning("graph.projection_failed", stage="nodes", error=message)
            if self._strict:
                raise
            return report

        if plan.relationships:
            try:
                report.relationships_written = self._store.upsert_relationships(plan.relationships)
            except Exception as exc:  # noqa: BLE001
                message = f"relationship projection failed: {type(exc).__name__}: {exc}"
                report.errors.append(message)
                logger.warning("graph.projection_failed", stage="relationships", error=message)
                if self._strict:
                    raise

        logger.info("graph.projected", **report.counts)
        return report

    def close_edge(self, fact_id: UUID | str, at: datetime) -> bool:
        """ADR-0005: set ``valid_to`` on an edge. The edge is never deleted."""
        return self._store.invalidate_relationship(fact_id, at)

    def close_edges(self, fact_ids: Sequence[UUID | str], at: datetime) -> int:
        return sum(1 for fact_id in fact_ids if self.close_edge(fact_id, at))


def merge_nodes(plan: ProjectionPlan) -> ProjectionPlan:
    """Collapse nodes that share an id, merging their properties. Mutates and returns ``plan``.

    The structural layer and the semantic layer legitimately describe the same node: a registry
    project whose Tier 0 seed also produced an ``entities`` row is one ``:Project``, written once from
    ``projects`` (registry name, status, parent) and once from ``entities`` (canonical name, summary,
    normalized name). Without this, the second ``MERGE`` would simply overwrite the first one's
    properties with nothing where it has nothing to say.

    First occurrence wins the *label*; later occurrences win a *property* they actually set (``None``
    is already stripped by :func:`~aimemory.providers.graph.projection._clean`, so "later wins" can
    never blank a value the earlier node had).
    """
    merged: dict[str, GraphNode] = {}
    for node in plan.nodes:
        key = str(node.id)
        existing = merged.get(key)
        if existing is None:
            merged[key] = node
            continue
        merged[key] = existing.model_copy(
            update={"properties": {**existing.properties, **node.properties}}
        )
    plan.nodes = list(merged.values())
    return plan


def prune_dangling(plan: ProjectionPlan) -> ProjectionPlan:
    """Drop edges whose endpoints are not in ``plan.nodes``. Mutates and returns ``plan``.

    :meth:`Neo4jGraphStore.upsert_relationships` *raises* on a missing endpoint, and it writes the
    whole batch in one call - so one unprojectable edge would take every other edge down with it.
    A full rebuild replays the graph from an empty database, where "in the plan" and "in the graph"
    are the same thing, so this is exactly the set that cannot be written. Each one is recorded in
    ``plan.dropped`` with a reason and reported, never silently discarded.
    """
    known = {str(node.id) for node in plan.nodes}
    kept = []
    for rel in plan.relationships:
        missing = [
            side
            for side, value in (("from", str(rel.from_id)), ("to", str(rel.to_id)))
            if value not in known
        ]
        if missing:
            plan.dropped.append(
                (str(rel.fact_id), f"{rel.predicate} endpoint not projected ({'+'.join(missing)})")
            )
            continue
        kept.append(rel)
    plan.relationships = kept
    return plan


def wipe_projection(store: GraphStore, *, ontology: Ontology | None = None) -> int:
    """Delete every node this system projects, and nothing else. Returns the node count removed.

    Deliberately **not** :meth:`Neo4jGraphStore.clear`, which is ``MATCH (n) DETACH DELETE n``. The
    same Neo4j database also stores NeoDash's saved dashboards (``:_Neodash_Dashboard``), which this
    system did not create and must not destroy on a rebuild. The wipe is therefore scoped to the
    ontology's own stored labels (``ontology.md`` §5), which is also what A08's brief asks for:
    "wipe only labels/relationships this system created".

    ``DETACH DELETE`` removes the relationships along with the nodes, so no separate edge pass is
    needed: every relationship this system writes has both endpoints inside those labels.
    """
    onto = ontology or load_ontology()
    labels = list(onto.stored_labels)
    counted = store.query(
        "MATCH (n) WHERE any(l IN labels(n) WHERE l IN $labels) RETURN count(n) AS n",
        {"labels": labels},
    )
    total = int(counted[0]["n"]) if counted else 0
    if total:
        # `apply_schema` is the store's public "run these statements" door; `clear()` is too broad
        # (see above) and the driver itself is private. Labels are from the ontology, never user input.
        store.apply_schema([f"MATCH (n:`{label}`) DETACH DELETE n" for label in labels])
    logger.info("graph.wiped", nodes=total, labels=len(labels))
    return total


def _fold_property_updates(plan: ProjectionPlan, report: ProjectionReport) -> list[GraphNode]:
    """Literal-valued functional facts become properties of the subject node, not edges."""
    by_id: dict[str, GraphNode] = {str(node.id): node for node in plan.nodes}
    for update in plan.property_updates:
        node = by_id.get(update.node_id)
        if node is None:
            report.dropped.append(
                (update.fact_id, "functional property has no projected subject node")
            )
            continue
        merged: dict[str, Any] = {**node.properties, **update.properties}
        by_id[update.node_id] = node.model_copy(update={"properties": merged})
    return list(by_id.values())
