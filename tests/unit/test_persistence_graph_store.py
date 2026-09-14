"""P5-T02: `Neo4jGraphStore` unit tests - no live Neo4j required.

`neo4j.GraphDatabase.driver(...)` never connects eagerly (the bolt handshake happens on first
`session.run`), so these tests build a real `Neo4jGraphStore` against bogus connection parameters
and replace `_driver.session` with a fake to exercise the pure-Python logic: label/reltype quoting
(Cypher injection defence), the write-clause guard on `query()`, and - the case this file exists
for - that a relationship violating the ontology is rejected before any Cypher reaches the driver.

Live round-trip coverage (real MERGE, real constraints) is `tests/integration/test_graph_store.py`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from aimemory.common.errors import GraphStoreError, OntologyError
from aimemory.common.time import utc_now
from aimemory.domain.enums import EntityType, Predicate
from aimemory.domain.ports import GraphNode, GraphRelationship
from aimemory.persistence.graph_store import Neo4jGraphStore, _quote_label, _quote_reltype


def _store() -> Neo4jGraphStore:
    """A store whose driver never actually connects (construction is lazy in the neo4j driver)."""
    return Neo4jGraphStore("bolt://unit-test-host:7687", "neo4j", "not-a-real-password")


class _FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    def __iter__(self):
        return iter(self._records)

    def single(self):
        return self._records[0] if self._records else None

    def consume(self):
        return MagicMock()


class _FakeSession:
    """Replaces the real neo4j session; `run()` is scripted per test via `runs`."""

    def __init__(self, runs: list[_FakeResult]) -> None:
        self._runs = list(runs)
        self.queries: list[str] = []

    def run(self, cypher: str, *args: Any, **kwargs: Any) -> _FakeResult:
        self.queries.append(cypher)
        return self._runs.pop(0) if self._runs else _FakeResult([])


def _patch_driver(store: Neo4jGraphStore, fake_session: _FakeSession) -> None:
    @contextmanager
    def _session_cm(*args: Any, **kwargs: Any):
        yield fake_session

    store._driver.close()  # the real (never-connected) driver from _store(); avoid a GC warning
    store._driver = MagicMock()
    store._driver.session = _session_cm


# ---- label/reltype quoting (Cypher injection defence) -----------------------------------------


def test_quote_label_accepts_a_plain_label() -> None:
    assert _quote_label("Project") == "`Project`"


@pytest.mark.parametrize("bad", ["Project`) DETACH DELETE (n", "Pro ject", "", "1Project"])
def test_quote_label_rejects_unsafe_input(bad: str) -> None:
    with pytest.raises(GraphStoreError):
        _quote_label(bad)


@pytest.mark.parametrize("bad", ["USES`]->() DETACH DELETE (n)-[r:USES", "has space"])
def test_quote_reltype_rejects_unsafe_input(bad: str) -> None:
    with pytest.raises(GraphStoreError):
        _quote_reltype(bad)


# ---- query() refuses write clauses --------------------------------------------------------------


@pytest.mark.parametrize(
    "cypher",
    [
        "CREATE (n:Project {id: '1'})",
        "MATCH (n) SET n.x = 1",
        "MATCH (n) DETACH DELETE n",
        "MATCH (n) REMOVE n.x",
        "DROP CONSTRAINT project_id",
    ],
)
def test_query_refuses_write_clauses(cypher: str) -> None:
    store = _store()
    try:
        with pytest.raises(GraphStoreError):
            store.query(cypher)
    finally:
        store.close()


def test_query_allows_a_read_only_cypher() -> None:
    store = _store()
    fake = _FakeSession([_FakeResult([{"n": 1}])])
    _patch_driver(store, fake)
    rows = store.query("MATCH (n) RETURN count(n) AS n")
    assert rows == [{"n": 1}]


# ---- the reason this file exists: ontology validation rejects a bad edge ----------------------


def test_upsert_relationships_rejects_an_out_of_ontology_edge() -> None:
    """`STORED_ON` only permits `Source|Repository -> Device`; `Project -> Technology` must fail
    before any MERGE Cypher is sent (ontology.md §3, §5)."""
    store = _store()
    from_id, to_id = str(uuid4()), str(uuid4())
    now = utc_now().isoformat()
    # First run() answers `_endpoint_labels` (labels(n) lookup); MERGE must never be reached.
    fake = _FakeSession([_FakeResult([
        {"id": from_id, "labels": ["Project", "Entity"]},
        {"id": to_id, "labels": ["Technology", "Entity"]},
    ])])
    _patch_driver(store, fake)

    rel = GraphRelationship(
        fact_id=uuid4(),
        predicate=Predicate.STORED_ON,
        from_id=from_id,
        to_id=to_id,
        properties={"valid_from": now, "observed_at": now, "engine": "deterministic"},
    )
    with pytest.raises(OntologyError):
        store.upsert_relationships([rel])
    # only the label lookup happened - no MERGE was ever issued
    assert len(fake.queries) == 1
    assert "MATCH (n {id: id})" in fake.queries[0] or "labels(n)" in fake.queries[0]


def test_upsert_relationships_accepts_an_in_ontology_edge() -> None:
    store = _store()
    from_id, to_id = str(uuid4()), str(uuid4())
    now = utc_now().isoformat()
    fake = _FakeSession([
        _FakeResult([
            {"id": from_id, "labels": ["Project", "Entity"]},
            {"id": to_id, "labels": ["Technology", "Entity"]},
        ]),
        _FakeResult([]),
    ])
    _patch_driver(store, fake)

    rel = GraphRelationship(
        fact_id=uuid4(),
        predicate=Predicate.USES,
        from_id=from_id,
        to_id=to_id,
        properties={"valid_from": now, "observed_at": now, "engine": "deterministic"},
    )
    written = store.upsert_relationships([rel])
    assert written == 1
    assert len(fake.queries) == 2
    assert "MERGE (a)-[r:`USES`" in fake.queries[1]


def test_upsert_relationships_raises_when_an_endpoint_is_missing() -> None:
    store = _store()
    from_id, to_id = str(uuid4()), str(uuid4())
    now = utc_now().isoformat()
    fake = _FakeSession([_FakeResult([{"id": from_id, "labels": ["Project", "Entity"]}])])
    _patch_driver(store, fake)

    rel = GraphRelationship(
        fact_id=uuid4(), predicate=Predicate.USES, from_id=from_id, to_id=to_id,
        properties={"valid_from": now, "observed_at": now, "engine": "deterministic"},
    )
    with pytest.raises(GraphStoreError):
        store.upsert_relationships([rel])


# ---- node property contract ---------------------------------------------------------------------


def test_upsert_nodes_rejects_missing_required_properties() -> None:
    store = _store()
    fake = _FakeSession([])
    _patch_driver(store, fake)
    node = GraphNode(id=uuid4(), label=EntityType.PROJECT, properties={"name": "X"})  # no type/observed_at/engine
    with pytest.raises(OntologyError):
        store.upsert_nodes([node])


def test_upsert_nodes_groups_by_stored_label_and_secondary_entity_label() -> None:
    store = _store()
    fake = _FakeSession([_FakeResult([]), _FakeResult([])])
    _patch_driver(store, fake)
    now = utc_now().isoformat()
    project_node = GraphNode(
        id=uuid4(), label=EntityType.PROJECT,
        properties={"name": "X", "type": "Project", "observed_at": now, "engine": "deterministic"},
    )
    device_node = GraphNode(
        id=uuid4(), label=EntityType.DEVICE,
        properties={"name": "dev", "type": "Device", "observed_at": now, "engine": "deterministic"},
    )
    written = store.upsert_nodes([project_node, device_node])
    assert written == 2
    assert len(fake.queries) == 2
    project_query, device_query = fake.queries
    assert "`Project`:Entity" in project_query
    assert "`Device`:Entity" not in device_query and "`Device`" in device_query


def test_upsert_empty_sequences_are_no_ops() -> None:
    store = _store()
    try:
        assert store.upsert_nodes([]) == 0
        assert store.upsert_relationships([]) == 0
    finally:
        store.close()
