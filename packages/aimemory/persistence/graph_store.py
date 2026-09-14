"""``Neo4jGraphStore`` - the :class:`~aimemory.domain.ports.GraphStore` port implementation
(ADR-0001: Neo4j is a rebuildable projection, never the sole holder of a fact).

Consumers: A08 (writes the structural and semantic projection during ingestion), A09 (graph
expansion, read-only), `cli/migrate.py` (applies ``constraints.cypher`` and the read-only user),
`scripts/rebuild-graph` (drives :meth:`Neo4jGraphStore.rebuild`).

Every write is an idempotent ``MERGE`` keyed on the PostgreSQL id (ontology.md §2/§3), grouped by
label/predicate so Cypher never needs a parameterised label (Neo4j forbids that). Every edge write
validates against :class:`~aimemory.ontology.loader.Ontology` before it reaches the database: since
:class:`~aimemory.domain.ports.GraphRelationship` does not carry the endpoint labels (only ids), this
implementation looks the current stored label of each endpoint up in the same transaction and calls
:meth:`Ontology.validate_relationship` before the ``MERGE`` - "before every edge write" (ontology.md
§5) means at write time against the graph's actual state, not against a label the caller merely
claims.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Self

import neo4j
from neo4j import GraphDatabase

from ..common.errors import GraphStoreError, OntologyError
from ..common.time import ensure_utc
from ..domain.enums import Predicate
from ..domain.ports import GraphNode, GraphRelationship
from ..ontology.loader import Ontology, load_ontology

__all__ = ["Neo4jGraphStore"]

#: Registry-only labels never carry the :Entity secondary label (ontology.md §5).
_NO_ENTITY_LABEL = {"Device", "Source"}

#: Cypher keywords that make a "read" query a write - `query()` refuses them (GraphStore.query
#: contract: "must refuse write clauses on a read-only session").
_WRITE_CLAUSE_RE = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL\s+apoc\.\w*\.(create|merge|remove))\b",
    re.IGNORECASE,
)


def _quote_label(label: str) -> str:
    """Backtick-quote a label; Cypher labels can never be bind parameters."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
        raise GraphStoreError("Refusing an unsafe label.", detail=label)
    return f"`{label}`"


def _quote_reltype(predicate: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", predicate):
        raise GraphStoreError("Refusing an unsafe relationship type.", detail=predicate)
    return f"`{predicate}`"


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return ensure_utc(value).isoformat()
    return value


class Neo4jGraphStore:
    """Synchronous ``neo4j`` driver-backed :class:`~aimemory.domain.ports.GraphStore`."""

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        *,
        database: str = "neo4j",
        ontology: Ontology | None = None,
    ) -> None:
        self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database
        self._ontology = ontology or load_ontology()

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- schema -----------------------------------------------------------------------------

    def apply_schema(self, statements: Sequence[str]) -> None:
        """Apply ``constraints.cypher``. Each ``CREATE ... IF NOT EXISTS`` statement is idempotent."""
        with self._driver.session(database=self._database) as session:
            for statement in statements:
                stripped = statement.strip()
                if not stripped:
                    continue
                session.run(stripped)

    # ---- nodes --------------------------------------------------------------------------------

    def upsert_nodes(self, nodes: Sequence[GraphNode]) -> int:
        if not nodes:
            return 0
        grouped: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            stored = self._ontology.stored_label(node.label)
            props = {k: _iso(v) for k, v in node.properties.items()}
            props["id"] = str(node.id)
            missing = self._ontology.node_properties.missing(props)
            if missing:
                raise OntologyError(
                    "Node payload is missing required properties.",
                    detail=f"label={stored} missing={missing}",
                )
            grouped.setdefault(stored, []).append({"id": str(node.id), "props": props})

        written = 0
        with self._driver.session(database=self._database) as session:
            for stored_label, rows in grouped.items():
                secondary = "" if stored_label in _NO_ENTITY_LABEL else ":Entity"
                cypher = (
                    f"UNWIND $rows AS row "
                    f"MERGE (n:{_quote_label(stored_label)}{secondary} {{id: row.id}}) "
                    f"SET n += row.props, n.updated_at = datetime()"
                )
                session.run(cypher, rows=rows)
                written += len(rows)
        return written

    # ---- relationships --------------------------------------------------------------------------

    def _endpoint_labels(
        self, session: neo4j.Session, ids: Sequence[str]
    ) -> dict[str, str]:
        """Resolve each id to its ontology (stored) label, ignoring the secondary ``:Entity``."""
        if not ids:
            return {}
        result = session.run(
            "UNWIND $ids AS id MATCH (n {id: id}) RETURN id, labels(n) AS labels", ids=list(ids)
        )
        stored_labels = set(self._ontology.stored_labels)
        resolved: dict[str, str] = {}
        for record in result:
            candidates = [lbl for lbl in record["labels"] if lbl in stored_labels]
            if candidates:
                resolved[record["id"]] = candidates[0]
        return resolved

    def upsert_relationships(self, relationships: Sequence[GraphRelationship]) -> int:
        if not relationships:
            return 0
        with self._driver.session(database=self._database) as session:
            ids = {str(r.from_id) for r in relationships} | {str(r.to_id) for r in relationships}
            labels = self._endpoint_labels(session, list(ids))

            grouped: dict[str, list[dict[str, Any]]] = {}
            for rel in relationships:
                predicate = rel.predicate.value if isinstance(rel.predicate, Predicate) else str(rel.predicate)
                from_id, to_id = str(rel.from_id), str(rel.to_id)
                from_label = labels.get(from_id)
                to_label = labels.get(to_id)
                if from_label is None or to_label is None:
                    raise GraphStoreError(
                        "Relationship endpoint does not exist in the graph.",
                        detail=f"{predicate}: from={from_id!r} to={to_id!r}",
                    )
                self._ontology.validate_relationship(predicate, from_label, to_label)
                props = {k: _iso(v) for k, v in rel.properties.items()}
                props["fact_id"] = str(rel.fact_id)
                missing = self._ontology.relationship_properties.missing(props)
                if missing:
                    raise OntologyError(
                        "Relationship payload is missing required properties.",
                        detail=f"{predicate} missing={missing}",
                    )
                grouped.setdefault(predicate, []).append(
                    {"from_id": from_id, "to_id": to_id, "fact_id": str(rel.fact_id), "props": props}
                )

            written = 0
            for predicate, rows in grouped.items():
                cypher = (
                    "UNWIND $rows AS row "
                    "MATCH (a {id: row.from_id}), (b {id: row.to_id}) "
                    f"MERGE (a)-[r:{_quote_reltype(predicate)} {{fact_id: row.fact_id}}]->(b) "
                    "SET r += row.props"
                )
                session.run(cypher, rows=rows)
                written += len(rows)
        return written

    def invalidate_relationship(self, fact_id: Any, at: datetime) -> bool:
        """ADR-0005: set ``valid_to`` on the edge; never delete it. Idempotent."""
        with self._driver.session(database=self._database) as session:
            result = session.run(
                "MATCH ()-[r {fact_id: $fact_id}]->() SET r.valid_to = datetime($at) "
                "RETURN count(r) AS n",
                fact_id=str(fact_id),
                at=ensure_utc(at).isoformat(),
            )
            record = result.single()
            return bool(record and record["n"] > 0)

    # ---- reads ------------------------------------------------------------------------------

    def neighbours(
        self,
        entity_ids: Sequence[Any],
        *,
        predicates: Sequence[Predicate] | None = None,
        depth: int = 1,
        limit: int = 25,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        depth = max(1, min(depth, 2))
        pred_values = [p.value if isinstance(p, Predicate) else str(p) for p in predicates] if predicates else None
        as_of_iso = ensure_utc(as_of).isoformat() if as_of else None
        cypher = (
            f"MATCH (n)-[r*1..{depth}]-(m) WHERE n.id IN $ids AND n <> m "
            "AND ($predicates IS NULL OR all(rel IN r WHERE type(rel) IN $predicates)) "
            "AND ($as_of IS NULL OR all(rel IN r WHERE rel.valid_from <= datetime($as_of) "
            "AND (rel.valid_to IS NULL OR rel.valid_to > datetime($as_of)))) "
            "RETURN DISTINCT m.id AS id, labels(m) AS labels, m.name AS name, "
            "m.project_id AS project_id, m.summary AS summary "
            "LIMIT $limit"
        )
        with self._driver.session(database=self._database, default_access_mode=neo4j.READ_ACCESS) as session:
            result = session.run(
                cypher,
                ids=[str(i) for i in entity_ids],
                predicates=pred_values,
                as_of=as_of_iso,
                limit=limit,
            )
            return [dict(record) for record in result]

    def query(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if _WRITE_CLAUSE_RE.search(cypher):
            raise GraphStoreError("Refusing a write clause on a read query.", detail=cypher[:200])
        with self._driver.session(database=self._database, default_access_mode=neo4j.READ_ACCESS) as session:
            result = session.run(cypher, parameters or {})
            return [dict(record) for record in result]

    # ---- lifecycle --------------------------------------------------------------------------

    def clear(self) -> None:
        """Delete every projected node/edge. Only ``scripts/rebuild-graph`` may call this."""
        with self._driver.session(database=self._database) as session:
            session.run("MATCH (n) DETACH DELETE n")

    def delete_by_source(self, source_id: Any) -> int:
        """Remove everything projected from one source, for a targeted rebuild after a re-scan.

        Not part of the frozen :class:`~aimemory.domain.ports.GraphStore` protocol - an extra
        primitive A08's rebuild driver composes with :meth:`upsert_nodes`/:meth:`upsert_relationships`.
        """
        with self._driver.session(database=self._database) as session:
            result = session.run(
                "MATCH (n {source_id: $sid}) DETACH DELETE n RETURN count(n) AS n",
                sid=str(source_id),
            )
            record = result.single()
            return int(record["n"]) if record else 0

    def rebuild(self, statements: Sequence[str]) -> None:
        """Entry point A08 drives from PostgreSQL (ontology.md §6/§7): re-apply the schema and
        clear the projection so a fresh, ordered replay of nodes-then-relationships can begin.

        The replay itself (reading every table in dependency order and calling
        :meth:`upsert_nodes`/:meth:`upsert_relationships`) is A08's ``knowledge/`` job - this method
        only owns the two steps that must happen on the Neo4j side first.
        """
        self.apply_schema(statements)
        self.clear()

    def health(self) -> bool:
        try:
            with self._driver.session(database=self._database) as session:
                result = session.run("RETURN 1 AS ok")
                record = result.single()
                return bool(record and record["ok"] == 1)
        except Exception:  # noqa: BLE001 - a health check must never raise, only report
            return False
