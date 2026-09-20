"""``aimemory.providers.graph`` - the Neo4j projection side of A08.

:class:`~aimemory.persistence.graph_store.Neo4jGraphStore` (A04) is the driver; this package is the
*mapping*: PostgreSQL rows to ontology-valid nodes and edges
(:mod:`~aimemory.providers.graph.projection`), and the ordered write that respects the store's
endpoint-label lookup (:mod:`~aimemory.providers.graph.writer`).

Neo4j is a rebuildable projection (ADR-0001). Nothing here is the system of record, and nothing here
may be the only place a fact exists.
"""

from ...persistence.graph_store import Neo4jGraphStore
from .factory import open_graph_store
from .projection import (
    ARTIFACT_LABELS,
    FUNCTIONAL_NODE_PROPERTY,
    NodePropertyUpdate,
    ProjectionPlan,
    artifact_label,
    artifact_node,
    entity_node,
    fact_edges,
    registry_node,
    structural_edge,
    supersedes_edge,
)
from .writer import (
    GraphProjector,
    ProjectionReport,
    merge_nodes,
    prune_dangling,
    wipe_projection,
)

__all__ = [
    "ARTIFACT_LABELS",
    "FUNCTIONAL_NODE_PROPERTY",
    "GraphProjector",
    "Neo4jGraphStore",
    "NodePropertyUpdate",
    "ProjectionPlan",
    "ProjectionReport",
    "artifact_label",
    "artifact_node",
    "entity_node",
    "fact_edges",
    "merge_nodes",
    "open_graph_store",
    "prune_dangling",
    "registry_node",
    "structural_edge",
    "supersedes_edge",
    "wipe_projection",
]
