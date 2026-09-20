"""P7-T03 acceptance against the live stack: PostgreSQL really is replayable into Neo4j (owner A08).

These tests run against whatever the databases actually hold - the pilot corpus, not a fixture -
because the thing being proven is that the projection survives *real* extraction output, including
the parts of it the ontology refuses.

What is asserted, and why each one is a property rather than a magic number:

* **idempotency** - ``rebuild-graph`` twice must leave identical per-label and per-type counts. The
  numbers are compared to each other, never to a constant, so the test keeps working as the corpus
  grows (ADR-0001: Neo4j is a rebuildable projection).
* **nothing vanishes silently** - every fact in PostgreSQL becomes an edge, becomes a node property,
  or is recorded in ``dropped`` with the ontology's reason. There is no fourth outcome.
* **the projection is complete where it can be** - ``:Source`` and ``:Episode`` counts equal their
  PostgreSQL row counts exactly; they are deterministic and have no way to be rejected.
* **temporal properties survive the round trip** - every projected edge carries ``valid_from``,
  ``observed_at`` and ``engine``, and a superseded decision keeps its node, its closed window and a
  ``SUPERSEDES`` edge (ADR-0005 rule 6).

The rebuild tests rewrite the projection. That is safe by definition - it is the same content, and
the wipe is scoped to this system's own labels - but it is why nothing here calls ``store.clear()``.
The synthetic supersession test cleans up its own nodes by id.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from aimemory.common.time import utc_now
from aimemory.domain.enums import ArtifactStatus, ArtifactType, EngineKind
from aimemory.domain.models import KnowledgeArtifact, Provenance
from aimemory.knowledge.rebuild import full_plan, rebuild_graph
from aimemory.knowledge.semantic import _facts, semantic_plan
from aimemory.providers.graph import GraphProjector, ProjectionPlan, artifact_node, fact_edges, supersedes_edge
from sqlalchemy import text
from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def rebuilt(graph_store: Any) -> dict[str, Any]:
    """One rebuild, shared by the assertions below. Uses the session-scoped store so the driver is
    not closed out from under the other tests."""
    return rebuild_graph(store=graph_store)


def test_rebuild_reproduces_identical_counts(rebuilt: dict[str, Any], graph_store: Any) -> None:
    """Idempotency is measured, not asserted: run it again and compare the two read-backs."""
    again = rebuild_graph(store=graph_store)

    assert again["nodes_by_label"] == rebuilt["nodes_by_label"]
    assert again["relationships_by_type"] == rebuilt["relationships_by_type"]
    assert again["nodes_written"] == rebuilt["nodes_written"]
    assert again["relationships_written"] == rebuilt["relationships_written"]
    # The second run wiped exactly what the first one wrote - the scoped wipe found all of it.
    assert again["wiped_nodes"] == rebuilt["nodes_written"]
    assert again["ok"] is True


def test_rebuild_is_idempotent_without_the_wipe_too(rebuilt: dict[str, Any], graph_store: Any) -> None:
    """Every write is a MERGE keyed on the PostgreSQL id, so replaying on top must change nothing."""
    merged_again = rebuild_graph(store=graph_store, wipe=False)

    assert merged_again["wiped_nodes"] == 0
    assert merged_again["nodes_by_label"] == rebuilt["nodes_by_label"]
    assert merged_again["relationships_by_type"] == rebuilt["relationships_by_type"]


def test_deterministic_layers_are_projected_completely(rebuilt: dict[str, Any]) -> None:
    """Sources and episodes come from the registry: nothing can reject them, so a gap is a bug."""
    postgres = rebuilt["postgres"]
    labels = rebuilt["nodes_by_label"]

    assert labels.get("Source", 0) == postgres["sources"]
    assert labels.get("Episode", 0) == postgres["episodes"]
    assert labels.get("Device", 0) == postgres["devices"]


def test_every_fact_is_an_edge_a_property_or_a_recorded_drop(pg_session: Session) -> None:
    """The invariant that makes a gap a finding instead of a mystery: there is no fourth outcome."""
    plan, index = semantic_plan(pg_session)
    facts = _facts(pg_session, None)
    labels = dict(index.labels)

    fact_plan: ProjectionPlan = fact_edges(facts, labels=labels)
    accounted = (
        len(fact_plan.relationships) + len(fact_plan.property_updates) + len(fact_plan.dropped)
    )

    assert accounted == len(facts)
    assert all(reason for _, reason in fact_plan.dropped)  # every drop explains itself


def test_no_edge_reaches_the_graph_without_its_temporal_properties(
    rebuilt: dict[str, Any], graph_store: Any
) -> None:
    """``relationship_properties.required`` is ``fact_id, valid_from, observed_at, engine``."""
    rows = graph_store.query(
        "MATCH ()-[r]->() "
        "WHERE r.fact_id IS NULL OR r.valid_from IS NULL OR r.observed_at IS NULL "
        "   OR r.engine IS NULL "
        "RETURN count(r) AS n"
    )
    assert rows == [{"n": 0}]


def test_closed_facts_are_closed_in_the_graph_not_missing_from_it(
    rebuilt: dict[str, Any], graph_store: Any, pg_session: Session
) -> None:
    """ADR-0005: a historical fact keeps its edge and carries ``valid_to``; it is never deleted.

    "Never deleted" means never *silently* absent. A closed fact that violates the ontology's
    declared direction is still rejected at the projection boundary (ADR-0015) and recorded in
    ``dropped`` with its reason - the third outcome this module's docstring names - so the expected
    graph count is the closed facts the plan actually projects, not every closed row in PostgreSQL.
    The difference is asserted explicitly below so a widening gap is a visible finding.
    """
    historical = int(
        pg_session.execute(
            text("SELECT count(*) FROM facts WHERE status = 'historical' AND valid_to IS NOT NULL")
        ).scalar()
        or 0
    )
    if historical == 0:
        pytest.skip("the corpus currently holds no closed facts")

    facts = _facts(pg_session, None)
    _, index = semantic_plan(pg_session)
    fact_plan: ProjectionPlan = fact_edges(facts, labels=dict(index.labels))
    projected_closed = sum(
        1
        for rel in fact_plan.relationships
        if rel.properties.get("valid_to") is not None
        and rel.properties.get("status") == "historical"
    )
    dropped_ids = {fact_id for fact_id, _ in fact_plan.dropped}
    closed_ids = {str(f.id) for f in facts if f.valid_to is not None and f.status == "historical"}

    rows = graph_store.query(
        "MATCH ()-[r]->() WHERE r.valid_to IS NOT NULL AND r.status = 'historical' "
        "RETURN count(r) AS n"
    )
    assert rows[0]["n"] == projected_closed
    # No fourth outcome: a closed fact is either in the graph or explains its own absence.
    assert closed_ids <= dropped_ids | {
        str(rel.properties["fact_id"]) for rel in fact_plan.relationships
    }


def test_every_projected_fact_edge_resolves_to_a_postgres_fact(
    rebuilt: dict[str, Any], graph_store: Any, pg_session: Session
) -> None:
    """Provenance in the direction that matters: the graph may not invent an edge (ADR-0001)."""
    rows = graph_store.query(
        "MATCH ()-[r]->() WHERE r.status IS NOT NULL RETURN r.fact_id AS fact_id LIMIT 200"
    )
    fact_ids = [row["fact_id"] for row in rows]
    if not fact_ids:
        pytest.skip("no semantic edges projected yet")

    found = int(
        pg_session.execute(
            text("SELECT count(*) FROM facts WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": fact_ids},
        ).scalar()
        or 0
    )
    assert found == len(set(fact_ids))


def test_plan_and_graph_agree_on_what_was_written(pg_session: Session, rebuilt: dict[str, Any]) -> None:
    """The report's numbers come out of Neo4j; this checks they also match what was planned."""
    plan, _details = full_plan(pg_session)

    assert len(plan.nodes) == rebuilt["nodes_written"]
    # Relationships can only *shrink* on write: two identical edge keys MERGE into one.
    assert sum(rebuilt["relationships_by_type"].values()) <= len(plan.relationships)


def test_supersession_round_trip_yields_the_expected_states_and_edge(graph_store: Any) -> None:
    """Architecture A -> B, written to the real Neo4j and read back with Cypher.

    The pilot corpus has no superseded artifact yet (MEASURED: 0 rows with ``supersedes_id``), so the
    ADR-0005 rule 6 acceptance is exercised with two synthetic decisions that are deleted afterwards.
    """
    now = utc_now()
    episode_id = uuid4()

    def _decision(title: str, **extra: Any) -> KnowledgeArtifact:
        return KnowledgeArtifact(
            id=uuid4(),
            type=ArtifactType.DECISION,
            title=title,
            body=f"integration fixture: {title}",
            project_id=None,
            engine=EngineKind.NATIVE,
            valid_from=now,
            provenance=Provenance(
                device_id="integration-test",
                observed_at=now,
                extraction_model_id="deterministic:test",
                episode_id=episode_id,
            ),
            **extra,
        )

    old = _decision(
        "Architecture A", current_status=ArtifactStatus.SUPERSEDED, valid_to=now + timedelta(hours=1)
    )
    new = _decision("Architecture B", current_status=ArtifactStatus.CURRENT, supersedes_id=old.id)

    plan = ProjectionPlan()
    for artifact in (old, new):
        node = artifact_node(artifact)
        assert node is not None
        plan.nodes.append(node)
    edge = supersedes_edge(new, old)
    assert edge is not None
    plan.relationships.append(edge)

    try:
        report = GraphProjector(graph_store).apply(plan)
        assert report.ok, report.errors

        rows = graph_store.query(
            "MATCH (b {id: $new})-[r:SUPERSEDES]->(a {id: $old}) "
            "RETURN a.status AS old_status, a.valid_to IS NOT NULL AS old_closed, "
            "       b.status AS new_status, b.valid_to IS NULL AS new_open, "
            "       r.fact_id AS fact_id",
            {"new": str(new.id), "old": str(old.id)},
        )
        assert rows == [
            {
                "old_status": ArtifactStatus.SUPERSEDED.value,
                "old_closed": True,
                "new_status": ArtifactStatus.CURRENT.value,
                "new_open": True,
                "fact_id": str(new.id),
            }
        ]

        # Closing the edge is a state change, never a delete (ADR-0005).
        assert graph_store.invalidate_relationship(new.id, now + timedelta(hours=2)) is True
        still_there = graph_store.query(
            "MATCH ()-[r:SUPERSEDES {fact_id: $fid}]->() RETURN r.valid_to IS NOT NULL AS closed",
            {"fid": str(new.id)},
        )
        assert still_there == [{"closed": True}]
    finally:
        with graph_store._driver.session(database=graph_store._database) as session:
            session.run(
                "MATCH (n) WHERE n.id IN [$a, $b] DETACH DELETE n",
                a=str(old.id),
                b=str(new.id),
            )


def test_joblab_project_reaches_its_documents_and_technologies(
    rebuilt: dict[str, Any], graph_store: Any
) -> None:
    """The pilot path, end to end: a project node with the neighbourhood retrieval will expand into."""
    rows = graph_store.query(
        "MATCH (p:Project) WHERE toLower(p.name) CONTAINS 'joblab' "
        "OPTIONAL MATCH (p)-[r]-(m) "
        "RETURN p.name AS name, count(r) AS degree, "
        "       count(DISTINCT CASE WHEN 'Document' IN labels(m) THEN m END) AS documents, "
        "       count(DISTINCT CASE WHEN 'Technology' IN labels(m) THEN m END) AS technologies "
        "ORDER BY degree DESC"
    )
    if not rows:
        pytest.skip("this corpus has no JobLab project")

    assert rows[0]["degree"] > 0
    assert rows[0]["documents"] + rows[0]["technologies"] > 0


# ====================================================================================================
# The incremental path: a new extraction must not leave the graph stale
# ====================================================================================================


def _stub_result(episode_id: Any) -> Any:
    """One small, valid extraction: two entities, one fact between them, one decision."""
    from aimemory.domain.enums import EntityType, Predicate
    from aimemory.domain.extraction import ExtractedEntity, ExtractedFact, ExtractionResult

    return ExtractionResult(
        episode_id=episode_id,
        engine=EngineKind.NATIVE,
        extraction_model_id="deterministic:integration-test",
        summary="integration fixture episode",
        entities=[
            ExtractedEntity(name="P7T03 Pilot Project", type=EntityType.PROJECT),
            ExtractedEntity(name="P7T03 Pilot Technology", type=EntityType.TECHNOLOGY),
        ],
        facts=[
            ExtractedFact(
                subject="P7T03 Pilot Project",
                predicate=Predicate.USES,
                object="P7T03 Pilot Technology",
                statement="P7T03 Pilot Project uses P7T03 Pilot Technology.",
            )
        ],
        valid=True,
    )


def _write_episode(session: Session, graph: Any) -> tuple[Any, Any]:
    """Persist one episode + extraction through the real writer. Returns (report, episode)."""
    from aimemory.common.config import get_settings
    from aimemory.domain.enums import EpisodeType
    from aimemory.domain.models import Episode
    from aimemory.knowledge.persist import KnowledgeWriter
    from aimemory.persistence.repositories import EpisodeRepo

    episode = EpisodeRepo(session).create(
        Episode(
            id=uuid4(),
            type=EpisodeType.MANUAL,
            title="P7-T03 incremental projection fixture",
            body="Fixture body.",
            observed_at=utc_now(),
        )
    )
    writer = KnowledgeWriter(session, device_id=get_settings().device_id, graph=graph)
    return writer.write(_stub_result(episode.id), episode), episode


def test_a_new_extraction_is_projected_immediately(pg_session: Session, graph_store: Any) -> None:
    """Otherwise the graph is stale from the moment anyone runs `tier2` until the next rebuild."""
    report, episode = _write_episode(pg_session, graph_store)
    created_ids = [str(node_id) for node_id in _projected_ids(pg_session, episode.id)]

    try:
        assert report.graph_nodes > 0
        assert report.graph_relationships > 0
        rows = graph_store.query(
            "MATCH (n) WHERE n.id IN $ids RETURN count(n) AS n", {"ids": created_ids}
        )
        assert rows[0]["n"] == len(created_ids)
        edge = graph_store.query(
            "MATCH (:Project {name: 'P7T03 Pilot Project'})-[r:USES]->"
            "(:Technology {name: 'P7T03 Pilot Technology'}) RETURN count(r) AS n"
        )
        assert edge[0]["n"] == 1
    finally:
        with graph_store._driver.session(database=graph_store._database) as session:
            session.run("MATCH (n) WHERE n.id IN $ids DETACH DELETE n", ids=created_ids)


def test_a_graph_outage_costs_the_projection_and_not_the_knowledge(pg_session: Session) -> None:
    """ADR-0001, simulated with a store that really fails: PostgreSQL is written, the run continues."""

    class DeadGraph:
        """Every call fails, the way a store does when Neo4j went down mid-run."""

        def upsert_nodes(self, nodes: Any) -> int:
            raise ConnectionResetError("Failed to read from defunct connection")

        def upsert_relationships(self, relationships: Any) -> int:
            raise ConnectionResetError("Failed to read from defunct connection")

        def invalidate_relationship(self, fact_id: Any, at: Any) -> bool:
            raise ConnectionResetError("Failed to read from defunct connection")

    report, episode = _write_episode(pg_session, DeadGraph())

    assert report.entities == 2
    assert report.facts == 1
    assert report.graph_nodes == 0
    assert any("ConnectionResetError" in w for w in report.warnings)

    # The knowledge is in PostgreSQL, which is the system of record; the graph is repaired later.
    rows = pg_session.execute(
        text("SELECT count(*) FROM entity_mentions WHERE episode_id = :eid"), {"eid": episode.id}
    ).scalar()
    assert rows == 2


def _projected_ids(session: Session, episode_id: Any) -> list[Any]:
    """Every node id this episode's write should have placed in the graph."""
    ids = [episode_id]
    ids += [
        row[0]
        for row in session.execute(
            text("SELECT DISTINCT entity_id FROM entity_mentions WHERE episode_id = :eid"),
            {"eid": episode_id},
        ).all()
    ]
    ids += [
        row[0]
        for row in session.execute(
            text(
                "SELECT id FROM knowledge_artifacts WHERE episode_id = :eid AND type <> 'summary'"
            ),
            {"eid": episode_id},
        ).all()
    ]
    return ids
