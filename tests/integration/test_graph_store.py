"""P5-T02 acceptance: `Neo4jGraphStore` round-trips a node and an edge against the real Neo4j, the
constraint script is idempotent when applied twice, and `SHOW CONSTRAINTS` matches the ontology's
18 stored labels (ontology.md §5).

Marked `integration`: needs the compose network. Every test cleans up the nodes/edges it creates by
id; nothing here calls `clear()` (which would wipe a shared dev graph).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from aimemory.common.config import get_settings
from aimemory.common.errors import OntologyError
from aimemory.common.time import utc_now
from aimemory.domain.enums import EntityType, Predicate
from aimemory.domain.ports import GraphNode, GraphRelationship
from aimemory.ontology import load_ontology
from aimemory.persistence.graph_store import Neo4jGraphStore
from neo4j.exceptions import ServiceUnavailable

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS_FILE = REPO_ROOT / "infra" / "neo4j" / "schema" / "constraints.cypher"


def _statements() -> list[str]:
    out = []
    for line in CONSTRAINTS_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        out.append(stripped.rstrip(";"))
    return out


@pytest.fixture(scope="module")
def store() -> Iterator[Neo4jGraphStore]:
    settings = get_settings()
    gs = Neo4jGraphStore(
        settings.neo4j.uri,
        settings.neo4j.user,
        settings.neo4j.password.get_secret_value(),
        database=settings.neo4j.database,
    )
    try:
        assert gs.health()
    except (ServiceUnavailable, AssertionError):
        gs.close()
        pytest.skip("Neo4j is not reachable from this test run.")
    yield gs
    gs.close()


def test_apply_schema_is_idempotent(store: Neo4jGraphStore) -> None:
    statements = _statements()
    store.apply_schema(statements)  # first application (may already exist from `migrate`)
    store.apply_schema(statements)  # second application must not raise


def test_constraints_cover_every_stored_label(store: Neo4jGraphStore) -> None:
    store.apply_schema(_statements())
    ontology = load_ontology()
    rows = store.query("SHOW CONSTRAINTS YIELD labelsOrTypes RETURN labelsOrTypes")
    declared = {label for row in rows for label in row["labelsOrTypes"]}
    assert declared == set(ontology.stored_labels)


def test_node_and_relationship_round_trip(store: Neo4jGraphStore) -> None:
    project_id, tech_id = str(uuid4()), str(uuid4())
    fact_id = str(uuid4())
    now = utc_now().isoformat()
    try:
        written = store.upsert_nodes([
            GraphNode(
                id=project_id, label=EntityType.PROJECT,
                properties={"name": "IT Project", "type": "Project", "observed_at": now,
                            "engine": "deterministic", "project_id": project_id},
            ),
            GraphNode(
                id=tech_id, label=EntityType.TECHNOLOGY,
                properties={"name": "IT Technology", "type": "Technology", "observed_at": now,
                            "engine": "deterministic"},
            ),
        ])
        assert written == 2

        rel_written = store.upsert_relationships([
            GraphRelationship(
                fact_id=fact_id, predicate=Predicate.USES, from_id=project_id, to_id=tech_id,
                properties={"valid_from": now, "observed_at": now, "engine": "deterministic"},
            )
        ])
        assert rel_written == 1

        rows = store.query(
            "MATCH (a {id: $a})-[r]->(b {id: $b}) RETURN type(r) AS t, r.fact_id AS fact_id",
            {"a": project_id, "b": tech_id},
        )
        assert rows == [{"t": "USES", "fact_id": fact_id}]

        assert store.invalidate_relationship(fact_id, utc_now()) is True

        closed = store.query(
            "MATCH ()-[r {fact_id: $fid}]->() RETURN r.valid_to IS NOT NULL AS closed",
            {"fid": fact_id},
        )
        assert closed == [{"closed": True}]
    finally:
        store.delete_by_source(project_id)  # harmless if no node has this as source_id
        with store._driver.session(database=store._database) as session:  # test-only teardown
            session.run("MATCH (n) WHERE n.id IN [$a, $b] DETACH DELETE n", a=project_id, b=tech_id)


def test_out_of_ontology_edge_is_rejected_end_to_end(store: Neo4jGraphStore) -> None:
    project_id, tech_id = str(uuid4()), str(uuid4())
    now = utc_now().isoformat()
    try:
        store.upsert_nodes([
            GraphNode(id=project_id, label=EntityType.PROJECT,
                      properties={"name": "Reject Project", "type": "Project", "observed_at": now,
                                  "engine": "deterministic"}),
            GraphNode(id=tech_id, label=EntityType.TECHNOLOGY,
                      properties={"name": "Reject Tech", "type": "Technology", "observed_at": now,
                                  "engine": "deterministic"}),
        ])
        bad = GraphRelationship(
            fact_id=uuid4(), predicate=Predicate.STORED_ON, from_id=project_id, to_id=tech_id,
            properties={"valid_from": now, "observed_at": now, "engine": "deterministic"},
        )
        with pytest.raises(OntologyError):
            store.upsert_relationships([bad])
        rows = store.query(
            "MATCH (a {id: $a})-[r]->(b {id: $b}) RETURN count(r) AS n",
            {"a": project_id, "b": tech_id},
        )
        assert rows == [{"n": 0}]
    finally:
        with store._driver.session(database=store._database) as session:
            session.run("MATCH (n) WHERE n.id IN [$a, $b] DETACH DELETE n", a=project_id, b=tech_id)


def test_health_check(store: Neo4jGraphStore) -> None:
    assert store.health() is True
