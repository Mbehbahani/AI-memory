"""``aimemory-ingest rebuild-graph`` - replay the whole Neo4j projection from PostgreSQL (A08).

ADR-0001 in one function. PostgreSQL is the system of record; Neo4j is a projection that can be
thrown away and rebuilt at any time, which is only true if something actually rebuilds it. The
driver is four steps:

1. read PostgreSQL - the deterministic layer (:func:`~aimemory.knowledge.structural.structural_plan`)
   and the engine-produced layer (:func:`~aimemory.knowledge.semantic.semantic_plan`);
2. reconcile the two into one plan - nodes described by both layers are merged, and edges whose
   endpoints are not in the plan are dropped *with a reason* rather than taking the whole batch down;
3. wipe the labels this system owns - never the whole database, because NeoDash keeps its saved
   dashboards in the same Neo4j instance (the uniqueness constraints are ``migrate``'s job, and they
   survive the wipe);
4. write nodes, then relationships.

**Idempotent by construction.** Every write is a ``MERGE`` keyed on the PostgreSQL id, and the wipe
means a second run starts from the same empty state as the first. Running it twice must leave
identical node and relationship counts - that is a property to *measure*, not to assert, so the
returned report carries the per-label and per-type counts read back out of Neo4j afterwards.

Time needs no closing pass here, and that is the point: a fact whose window is already closed in
PostgreSQL (``valid_to`` set, ``status=historical``) is *written* with that ``valid_to`` on the edge,
and a superseded artifact keeps its node, its ``valid_to`` and its ``SUPERSEDES`` edge (ADR-0005
rules 1/5/6). Nothing temporal is ever expressed by deleting something - ``closed_edges`` in the
report counts how many edges landed with a closed window, so the claim is checkable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.logging import get_logger
from ..common.time import utc_now
from ..ontology import Ontology, load_ontology
from ..providers.graph import GraphProjector, ProjectionPlan
from ..providers.graph.factory import open_graph_store
from ..providers.graph.writer import merge_nodes, prune_dangling, wipe_projection
from .semantic import semantic_plan
from .structural import structural_plan

__all__ = ["RebuildReport", "full_plan", "rebuild_graph"]

logger = get_logger(__name__)

#: Tables whose row counts are reported next to the projected counts, so a gap is visible in the
#: same output rather than requiring a second query.
_SOURCE_COUNTS = {
    "projects": "SELECT count(*) FROM projects",
    "devices": "SELECT count(*) FROM devices",
    "sources": "SELECT count(*) FROM sources",
    "documents": "SELECT count(*) FROM sources WHERE policy IN ('INDEX_CONTENT', 'MIRROR')",
    "episodes": "SELECT count(*) FROM episodes",
    "entities": "SELECT count(*) FROM entities WHERE merged_into_id IS NULL",
    "facts": "SELECT count(*) FROM facts",
    "artifacts": "SELECT count(*) FROM knowledge_artifacts",
    "entity_mentions": "SELECT count(*) FROM entity_mentions",
}


@dataclass(slots=True)
class RebuildReport:
    """MEASURED result of one rebuild. Everything here was counted, nothing estimated."""

    nodes_written: int = 0
    relationships_written: int = 0
    wiped_nodes: int = 0
    dropped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    closed_edges: int = 0
    unconfirmed_edges: int = 0
    postgres: dict[str, int] = field(default_factory=dict)
    nodes_by_label: dict[str, int] = field(default_factory=dict)
    relationships_by_type: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        """The JSON ``aimemory-ingest rebuild-graph`` prints."""
        by_reason: dict[str, int] = {}
        for _, reason in self.dropped:
            by_reason[reason] = by_reason.get(reason, 0) + 1
        return {
            "ok": self.ok,
            "wiped_nodes": self.wiped_nodes,
            "nodes_written": self.nodes_written,
            "relationships_written": self.relationships_written,
            "closed_edges": self.closed_edges,
            "unconfirmed_edges": self.unconfirmed_edges,
            "dropped": len(self.dropped),
            "dropped_by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
            "errors": self.errors,
            "postgres": self.postgres,
            "nodes_by_label": self.nodes_by_label,
            "relationships_by_type": self.relationships_by_type,
            "duration_ms": self.duration_ms,
        }


def full_plan(
    session: Session,
    *,
    ontology: Ontology | None = None,
    project_id: str | None = None,
) -> tuple[ProjectionPlan, dict[str, Any]]:
    """The whole projection as one plan, plus the per-layer numbers. Pure read."""
    onto = ontology or load_ontology()
    structural, structural_index = structural_plan(session, ontology=onto, project_id=project_id)
    semantic, semantic_index = semantic_plan(
        session, ontology=onto, project_id=project_id, known_labels=structural_index.labels
    )

    plan = ProjectionPlan()
    plan.extend(structural)
    plan.extend(semantic)
    merge_nodes(plan)
    prune_dangling(plan)

    details = {
        "structural": structural.counts,
        "semantic": semantic.counts,
        "unresolved_links": structural_index.unresolved_links,
        "entities_read": semantic_index.entities,
        "facts_read": semantic_index.facts,
        "artifacts_projected": semantic_index.artifacts,
        "artifacts_without_a_label": semantic_index.skipped_summaries,
        "merged": plan.counts,
    }
    return plan, details


def rebuild_graph(
    *,
    project_id: str | None = None,
    wipe: bool = True,
    store: Any = None,
    database: Any = None,
    ontology: Ontology | None = None,
) -> dict[str, Any]:
    """Re-project PostgreSQL into Neo4j and return MEASURED counts.

    Called by ``aimemory-ingest rebuild-graph`` (``cli/ingest.py``), which json-dumps the result.
    Raises when Neo4j is unreachable: unlike an extraction, a rebuild that cannot reach the graph has
    achieved nothing, and exiting non-zero is the honest answer.

    ``wipe=False`` re-projects on top of what is there. That is still correct - every write is a
    MERGE - but it cannot remove a node whose PostgreSQL row is gone, so the default is ``True``.
    """
    started = utc_now()
    onto = ontology or load_ontology()
    report = RebuildReport()

    owns_store = store is None
    graph = store or open_graph_store(required=True)
    assert graph is not None  # noqa: S101 - required=True raises instead of returning None

    db = database
    owns_db = False
    if db is None:
        from ..persistence.db import Database  # noqa: PLC0415 - keeps the import out of module load

        db = Database()
        owns_db = True

    try:
        with db.session() as session:
            plan, details = full_plan(session, ontology=onto, project_id=project_id)
            report.postgres = _postgres_counts(session)

        if wipe:
            report.wiped_nodes = wipe_projection(graph, ontology=onto)

        projector = GraphProjector(graph, strict=False)
        projection = projector.apply(plan)
        report.nodes_written = projection.nodes_written
        report.relationships_written = projection.relationships_written
        report.dropped = list(projection.dropped)
        report.errors = list(projection.errors)
        report.closed_edges = sum(
            1 for rel in plan.relationships if rel.properties.get("valid_to") is not None
        )
        report.unconfirmed_edges = sum(
            1 for rel in plan.relationships if rel.properties.get("status") == "unconfirmed"
        )

        report.nodes_by_label = _nodes_by_label(graph)
        report.relationships_by_type = _relationships_by_type(graph)
    finally:
        if owns_store:
            graph.close()
        if owns_db:
            db.dispose()

    report.duration_ms = int((utc_now() - started).total_seconds() * 1000)
    payload = report.as_dict() | {"plan": details, "project_id": project_id}
    logger.info(
        "graph.rebuilt",
        nodes=report.nodes_written,
        relationships=report.relationships_written,
        dropped=len(report.dropped),
        errors=len(report.errors),
        duration_ms=report.duration_ms,
    )
    return payload


# --------------------------------------------------------------------------------------------------
# Read-back (the numbers in the report come out of the databases, not out of the plan)
# --------------------------------------------------------------------------------------------------


def _postgres_counts(session: Session) -> dict[str, int]:
    return {name: int(session.execute(text(sql)).scalar() or 0) for name, sql in _SOURCE_COUNTS.items()}


def _nodes_by_label(graph: Any) -> dict[str, int]:
    rows = graph.query(
        "MATCH (n) UNWIND labels(n) AS label RETURN label, count(*) AS n ORDER BY label"
    )
    return {str(row["label"]): int(row["n"]) for row in rows}


def _relationships_by_type(graph: Any) -> dict[str, int]:
    rows = graph.query("MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n ORDER BY type")
    return {str(row["type"]): int(row["n"]) for row in rows}
