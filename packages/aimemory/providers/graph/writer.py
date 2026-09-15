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
from .projection import ProjectionPlan

__all__ = ["GraphProjector", "ProjectionReport"]

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
