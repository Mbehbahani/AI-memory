"""P10-T01 (A09): stage 4, graph expansion - seeds, neighbours and the mandatory degraded mode.

The headline requirement is plan section Y's failure test: **Neo4j down -> vector+keyword with a
warning, never a failed query**. That is asserted here by *simulating the outage* (a graph double
whose every call raises, exactly as the driver does when Bolt is unreachable), not by trusting a
docstring - and again at the API level in ``tests/integration/test_gateway_search.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from aimemory.domain.enums import EntityType, ObjectType
from aimemory.domain.retrieval import RetrievalConfig
from aimemory.retrieval.expansion import (
    GRAPH_DEGRADED_WARNING,
    expand,
    neighbours,
    seed_entity_ids,
)

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
CONFIG = RetrievalConfig(
    graph_relationship_types=["USES", "DEPENDS_ON", "RELATED_TO"], graph_max_nodes=25
)


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:p10-expansion:{label}")


CHUNK_A, CHUNK_B = _id("chunk-a"), _id("chunk-b")
ARTIFACT_A = _id("artifact-a")
ENTITY_1, ENTITY_2, ENTITY_3 = _id("entity-1"), _id("entity-2"), _id("entity-3")


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Result:
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._rows)

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class FakeSession:
    """Routes the two seed queries by a marker in their SQL."""

    def __init__(self, chunks: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> None:
        self.routes = {"FROM entity_mentions": chunks, "FROM artifact_entities": artifacts}
        self.executed: list[str] = []

    def execute(self, statement: Any, params: Any = None) -> _Result:
        sql = str(statement)
        self.executed.append(sql)
        for marker, rows in self.routes.items():
            if marker in sql:
                return _Result(rows)
        return _Result([])


class FakeGraph:
    """A :class:`GraphStore` double. ``down=True`` raises like an unreachable Bolt endpoint."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, *, down: bool = False) -> None:
        self.rows = rows or []
        self.down = down
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def query(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((cypher, parameters or {}))
        if self.down:
            raise ConnectionError("Unable to retrieve routing information")
        return list(self.rows)

    def health(self) -> bool:
        return not self.down


def _neighbour(entity_id: UUID, name: str, predicate: str = "USES", **extra: Any) -> dict[str, Any]:
    row = {
        "id": str(entity_id),
        "name": name,
        "type": "Technology",
        "labels": ["Technology", "Entity"],
        "project_id": "joblab-de",
        "predicate": predicate,
        "fact_id": str(_id(f"fact-{name}")),
        "confidence": 0.9,
        "valid_from": "2026-01-01T00:00:00+00:00",
        "valid_to": None,
        "outgoing": True,
    }
    row.update(extra)
    return row


def _session() -> FakeSession:
    return FakeSession(
        chunks=[
            {"object_id": str(CHUNK_A), "entity_id": str(ENTITY_1)},
            {"object_id": str(CHUNK_A), "entity_id": str(ENTITY_2)},
        ],
        artifacts=[{"object_id": str(ARTIFACT_A), "entity_id": str(ENTITY_2)}],
    )


KEYS = [
    (ObjectType.CHUNK, CHUNK_A),
    (ObjectType.CHUNK, CHUNK_B),
    (ObjectType.ARTIFACT, ARTIFACT_A),
]


def test_seeds_come_from_mentions_for_chunks_and_from_artifact_entities_for_artifacts() -> None:
    seeds = seed_entity_ids(_session(), KEYS)

    assert seeds[(ObjectType.CHUNK, CHUNK_A)] == sorted([ENTITY_1, ENTITY_2], key=str)
    assert seeds[(ObjectType.ARTIFACT, ARTIFACT_A)] == [ENTITY_2]
    # A hit with no mentions is present with an empty list, not missing - "we looked, there are none".
    assert seeds[(ObjectType.CHUNK, CHUNK_B)] == []


def test_a_hit_sharing_an_entity_with_a_neighbour_is_marked_entity_linked() -> None:
    graph = FakeGraph([_neighbour(ENTITY_3, "pgvector"), _neighbour(ENTITY_2, "Postgres")])

    result = expand(_session(), graph, KEYS, config=CONFIG, as_of=NOW)

    assert {item.name for item in result.related} == {"pgvector", "Postgres"}
    # ENTITY_2 is mentioned by chunk A and by the artifact, so both earn `entity_linked`.
    assert result.entity_linked_keys == {
        (ObjectType.CHUNK, CHUNK_A),
        (ObjectType.ARTIFACT, ARTIFACT_A),
    }
    assert result.warnings == []
    assert result.counts == {"graph_seeds": 2, "graph_related": 2, "graph_linked_hits": 2}


def test_neo4j_down_degrades_to_vector_plus_keyword_with_a_warning() -> None:
    """Plan section Y failure test, simulated: every graph call raises, as an outage does."""
    graph = FakeGraph(down=True)

    result = expand(_session(), graph, KEYS, config=CONFIG, as_of=NOW)

    assert result.warnings == [GRAPH_DEGRADED_WARNING]
    assert result.degraded is True
    assert result.related == []
    assert result.entity_linked_keys == set(), "no boost may be invented while the graph is down"
    # The PostgreSQL half survives: hits still know which entities they mention.
    assert result.entity_ids_by_hit[(ObjectType.CHUNK, CHUNK_A)] == sorted(
        [ENTITY_1, ENTITY_2], key=str
    )
    assert result.seed_entity_ids == sorted([ENTITY_1, ENTITY_2], key=str)


def test_expansion_disabled_is_not_a_degradation_and_emits_no_warning() -> None:
    graph = FakeGraph([_neighbour(ENTITY_3, "pgvector")])

    result = expand(_session(), graph, KEYS, config=CONFIG, as_of=NOW, enabled=False)

    assert result.warnings == []
    assert result.degraded is False
    assert graph.calls == [], "a disabled expansion must not touch Neo4j at all"
    assert result.entity_ids_by_hit[(ObjectType.CHUNK, CHUNK_A)], "seeds are still resolved"


def test_no_graph_configured_is_silent_too() -> None:
    result = expand(_session(), None, KEYS, config=CONFIG, as_of=NOW)

    assert result.related == []
    assert result.warnings == []


def test_the_cypher_is_bounded_and_carries_the_as_of_predicate() -> None:
    graph = FakeGraph([_neighbour(ENTITY_3, "pgvector")])

    expand(_session(), graph, KEYS, config=CONFIG, as_of=NOW, project_ids=["joblab-de"])

    cypher, params = graph.calls[0]
    assert params["max_nodes"] == CONFIG.graph_max_nodes == 25
    assert params["relationship_types"] == CONFIG.graph_relationship_types
    assert params["as_of"] == NOW.isoformat()
    assert params["project_ids"] == ["joblab-de"]
    assert "LIMIT $max_nodes" in cypher
    assert "r.valid_from" in cypher and "r.valid_to" in cypher
    assert "[r*" not in cypher, "V0.1 expansion is one hop (graph_expansion.max_depth = 1)"


def test_the_temporal_predicate_tolerates_both_stored_representations() -> None:
    """Edges are written with ISO-string ``valid_from`` and DateTime ``valid_to``; the predicate
    normalises both with ``datetime(toString(x))`` instead of comparing mismatched types."""
    graph = FakeGraph([_neighbour(ENTITY_3, "pgvector")])

    expand(_session(), graph, KEYS, config=CONFIG, as_of=NOW)

    cypher, _ = graph.calls[0]
    assert "datetime(toString(r.valid_from))" in cypher
    assert "datetime(toString(r.valid_to))" in cypher


def test_neighbours_are_deduplicated_and_capped_at_max_nodes() -> None:
    rows = [_neighbour(_id(f"n{i}"), f"name-{i}") for i in range(40)]
    rows += [_neighbour(ENTITY_3, "pgvector"), _neighbour(ENTITY_3, "pgvector")]
    graph = FakeGraph(rows)

    related = neighbours(graph, [ENTITY_1], config=CONFIG, as_of=NOW)

    assert len(related) <= CONFIG.graph_max_nodes
    identities = [(item.entity_id, item.predicate, item.direction) for item in related]
    assert len(identities) == len(set(identities))


@pytest.mark.parametrize(
    ("row_type", "labels", "expected"),
    [
        ("SubProject", ["Project", "Entity"], EntityType.SUB_PROJECT),
        (None, ["Technology", "Entity"], EntityType.TECHNOLOGY),
        ("NotAType", ["Decision", "Entity"], EntityType.DECISION),
    ],
)
def test_entity_type_prefers_the_type_property_then_falls_back_to_the_label(
    row_type: str | None, labels: list[str], expected: EntityType
) -> None:
    """The *stored label* of a SubProject is ``Project`` (ontology.md §5), so the property wins."""
    row = _neighbour(ENTITY_3, "thing")
    row["type"] = row_type
    row["labels"] = labels
    graph = FakeGraph([row])

    related = neighbours(graph, [ENTITY_1], config=CONFIG, as_of=NOW)

    assert related[0].type is expected


def test_a_node_without_a_uuid_id_is_skipped_rather_than_guessed_at() -> None:
    """Registry nodes (``Device``, ``Source``) carry slug ids; they are not citable entities."""
    row = _neighbour(ENTITY_3, "local-development-machine")
    row["id"] = "local-development-machine"
    graph = FakeGraph([row])

    assert neighbours(graph, [ENTITY_1], config=CONFIG, as_of=NOW) == []


def test_direction_is_reported_from_the_edge_not_assumed() -> None:
    graph = FakeGraph(
        [
            _neighbour(ENTITY_3, "out-one", outgoing=True),
            _neighbour(_id("n-in"), "in-one", outgoing=False),
        ]
    )

    related = neighbours(graph, [ENTITY_1], config=CONFIG, as_of=NOW)

    assert [item.direction for item in related] == ["out", "in"]
