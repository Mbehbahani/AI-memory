"""P7-T03 unit acceptance for the Neo4j projection driver (owner A08).

Covers the four things that are easy to get wrong and impossible to see from a count:

* **reconciliation** - the structural layer and the semantic layer describe some of the same nodes
  (a registry project and its Tier 0 ``entities`` row), and an edge whose endpoint was not projected
  must be dropped with a reason instead of taking the whole write batch down with it;
* **temporal fidelity (ADR-0005)** - a closed fact reaches Neo4j *already closed*, an ``unconfirmed``
  one keeps an open window and its flag, and a superseded decision keeps its node plus a
  ``SUPERSEDES`` edge. Nothing temporal is expressed by deleting anything;
* **the wipe is scoped** - ``rebuild-graph`` removes the labels this system owns and nothing else,
  because NeoDash stores its saved dashboards in the same database;
* **degradation (ADR-0001)** - PostgreSQL is the system of record, so a graph outage costs the
  projection and never the knowledge. Simulated here with a socket that really refuses, and with a
  store whose writes really raise - not with a docstring.

Everything runs with nothing running: no Postgres, no Neo4j.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from aimemory.common.config import get_settings
from aimemory.common.time import utc_now
from aimemory.domain.enums import (
    ArtifactStatus,
    ArtifactType,
    EngineKind,
    EntityType,
    FactStatus,
    Predicate,
)
from aimemory.domain.models import Entity, Fact, KnowledgeArtifact, Provenance
from aimemory.domain.ports import GraphNode
from aimemory.knowledge import get_writer
from aimemory.ontology import load_ontology
from aimemory.providers.graph import (
    GraphProjector,
    ProjectionPlan,
    artifact_node,
    entity_node,
    fact_edges,
    merge_nodes,
    open_graph_store,
    prune_dangling,
    registry_node,
    structural_edge,
    supersedes_edge,
)
from aimemory.providers.graph.writer import wipe_projection

NOW = utc_now()
DEVICE = "test-device"


@pytest.fixture(autouse=True)
def _usable_logger() -> Any:
    """Restore structlog's defaults before each test in this module.

    ``tests/unit/test_secrets.py`` calls ``configure_logging()`` while pytest's ``capsys`` owns
    ``sys.stdout``; ``PrintLoggerFactory(file=sys.stdout)`` binds that temporary file *permanently*
    and ``cache_logger_on_first_use=True`` caches the bound logger, so every structlog call made
    later in the same process raises ``ValueError: I/O operation on closed file``. The code under
    test here logs (``graph.unavailable``, ``graph.wiped``, ``graph.projection_failed``), which makes
    these the first unit tests to trip over it. Reported to A12 rather than worked around silently.
    """
    structlog.reset_defaults()
    return None

#: A port nothing listens on. Connecting is refused immediately, so an "outage" costs milliseconds
#: rather than a driver connect timeout.
DEAD_BOLT_URI = "bolt://127.0.0.1:1"


def _provenance(**extra: Any) -> Provenance:
    return Provenance(
        device_id=DEVICE,
        observed_at=NOW,
        extraction_model_id="stub:test",
        episode_id=extra.pop("episode_id", None),
        **extra,
    )


def _entity(name: str, type_: EntityType, entity_id: UUID | None = None) -> Entity:
    return Entity(
        id=entity_id or uuid4(),
        type=type_,
        canonical_name=name,
        normalized_name=name.lower(),
        project_id="joblab-de",
        engine=EngineKind.NATIVE,
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def _fact(
    subject: Entity,
    predicate: Predicate,
    obj: Entity,
    *,
    status: FactStatus = FactStatus.CURRENT,
    valid_to: Any = None,
    supersedes: UUID | None = None,
) -> Fact:
    return Fact(
        id=uuid4(),
        subject_entity_id=subject.id,
        predicate=predicate,
        object_entity_id=obj.id,
        statement=f"{subject.canonical_name} {predicate.value} {obj.canonical_name}",
        valid_from=NOW,
        valid_to=valid_to,
        observed_at=NOW,
        status=status,
        supersedes_fact_id=supersedes,
        engine=EngineKind.NATIVE,
        project_id="joblab-de",
        provenance=_provenance(valid_from=NOW),
    )


def _decision(title: str, *, valid_to: Any = None, status: ArtifactStatus, supersedes: UUID | None = None) -> KnowledgeArtifact:
    return KnowledgeArtifact(
        id=uuid4(),
        type=ArtifactType.DECISION,
        title=title,
        body=f"body of {title}",
        project_id="joblab-de",
        current_status=status,
        valid_from=NOW,
        valid_to=valid_to,
        supersedes_id=supersedes,
        engine=EngineKind.NATIVE,
        provenance=_provenance(episode_id=uuid4()),
    )


# ====================================================================================================
# Reconciling the two layers
# ====================================================================================================


def test_merge_nodes_combines_the_registry_and_entity_views_of_one_project() -> None:
    """A project is one node, not a registry node and an entity node that never meet."""
    node_id = uuid4()
    entity = _entity("JobLab DE", EntityType.PROJECT, entity_id=node_id)
    entity.summary = "Data engineering pilot"

    plan = ProjectionPlan()
    plan.nodes.append(
        registry_node(
            node_id,
            EntityType.PROJECT,
            "JobLab DE",
            observed_at=NOW,
            project_id="joblab-de",
            status="active",
        )
    )
    plan.nodes.append(entity_node(entity))

    merge_nodes(plan)

    assert len(plan.nodes) == 1
    merged = plan.nodes[0]
    assert merged.properties["status"] == "active"  # only the registry knows this
    assert merged.properties["summary"] == "Data engineering pilot"  # only the entity knows this
    assert merged.properties["normalized_name"] == "joblab de"


def test_prune_dangling_drops_an_edge_whose_endpoint_is_absent_and_says_why() -> None:
    """`upsert_relationships` raises on a missing endpoint and writes in one batch, so one
    unprojectable edge would otherwise cost every other edge in the run."""
    project = _entity("JobLab DE", EntityType.PROJECT)
    tech = _entity("dbt", EntityType.TECHNOLOGY)
    plan = ProjectionPlan()
    plan.nodes.append(entity_node(project))  # `tech` deliberately not projected
    edge = structural_edge(
        Predicate.USES,
        from_id=project.id,
        to_id=tech.id,
        from_label=EntityType.PROJECT,
        to_label=EntityType.TECHNOLOGY,
        observed_at=NOW,
        edge_id=f"uses:{project.id}:{tech.id}",
    )
    assert edge is not None  # the edge itself is legal; only its endpoint is missing
    plan.relationships.append(edge)

    prune_dangling(plan)

    assert plan.relationships == []
    assert len(plan.dropped) == 1
    assert "endpoint not projected" in plan.dropped[0][1]


# ====================================================================================================
# ADR-0005 in the projection
# ====================================================================================================


def test_closed_fact_reaches_the_graph_already_closed_and_linked_to_what_closed_it() -> None:
    project = _entity("JobLab DE", EntityType.PROJECT)
    old_tech = _entity("Snowflake", EntityType.TECHNOLOGY)
    new_tech = _entity("Databricks", EntityType.TECHNOLOGY)
    labels = {
        str(project.id): project.type,
        str(old_tech.id): old_tech.type,
        str(new_tech.id): new_tech.type,
    }

    closed = _fact(
        project,
        Predicate.SELECTED_OPTION,
        old_tech,
        status=FactStatus.HISTORICAL,
        valid_to=NOW + timedelta(days=1),
    )
    current = _fact(project, Predicate.SELECTED_OPTION, new_tech, supersedes=closed.id)

    plan = fact_edges([closed, current], labels=labels)

    by_fact = {str(rel.properties["fact_id"]): rel for rel in plan.relationships}
    assert len(by_fact) == 2
    old_edge = by_fact[str(closed.id)]
    new_edge = by_fact[str(current.id)]

    # A functional predicate with an entity object is a RELATED_TO edge tagged with the real
    # predicate (ontology.md §4) - it is never invented as its own relationship type.
    # SELECTED_OPTION, not USES_ARCHITECTURE: ADR-0015 made the latter an ordinary multi-valued
    # semantic edge, because a project uses several technologies at the same time. A genuine
    # architecture migration is a change of *selected option*, which is what supersedes.
    assert old_edge.predicate is Predicate.RELATED_TO
    assert old_edge.properties["predicate"] == Predicate.SELECTED_OPTION.value
    assert old_edge.properties["valid_to"] == NOW + timedelta(days=1)
    assert old_edge.properties["status"] == FactStatus.HISTORICAL.value
    # The new edge names the fact it closed, so the chain is queryable in the graph too.
    assert new_edge.properties["supersedes_fact_id"] == str(closed.id)
    assert "valid_to" not in new_edge.properties


def test_unconfirmed_fact_keeps_an_open_window_and_carries_its_flag() -> None:
    """ADR-0005 rule 3: a fact that a re-extraction did not repeat is flagged, not closed."""
    project = _entity("JobLab DE", EntityType.PROJECT)
    tech = _entity("dbt", EntityType.TECHNOLOGY)
    unconfirmed = _fact(project, Predicate.USES, tech, status=FactStatus.UNCONFIRMED)

    plan = fact_edges([unconfirmed], labels={str(project.id): project.type, str(tech.id): tech.type})

    (edge,) = plan.relationships
    assert edge.properties["status"] == FactStatus.UNCONFIRMED.value
    assert "valid_to" not in edge.properties  # still current for `as_of` (rule 5)


def test_supersession_fixture_yields_both_states_and_a_supersedes_edge() -> None:
    """Architecture A -> B: A keeps its node with a closed window, B is current, B SUPERSEDES A."""
    old = _decision("Architecture A", valid_to=NOW + timedelta(days=1), status=ArtifactStatus.SUPERSEDED)
    new = _decision("Architecture B", status=ArtifactStatus.CURRENT, supersedes=old.id)

    old_node = artifact_node(old)
    new_node = artifact_node(new)
    edge = supersedes_edge(new, old)

    assert old_node is not None and new_node is not None
    assert old_node.properties["status"] == ArtifactStatus.SUPERSEDED.value
    assert old_node.properties["valid_to"] == NOW + timedelta(days=1)
    assert new_node.properties["status"] == ArtifactStatus.CURRENT.value
    assert "valid_to" not in new_node.properties
    assert edge is not None
    assert edge.predicate is Predicate.SUPERSEDES
    assert (str(edge.from_id), str(edge.to_id)) == (str(new.id), str(old.id))


# ====================================================================================================
# The wipe is scoped to what this system created
# ====================================================================================================


@dataclass
class RecordingStore:
    """A :class:`~aimemory.domain.ports.GraphStore` double that records what it was asked to run."""

    node_count: int = 0
    statements: list[str] = field(default_factory=list)
    nodes: list[GraphNode] = field(default_factory=list)
    fail_with: Exception | None = None

    def apply_schema(self, statements: Any) -> None:
        self.statements.extend(statements)

    def upsert_nodes(self, nodes: Any) -> int:
        if self.fail_with is not None:
            raise self.fail_with
        self.nodes.extend(nodes)
        return len(list(nodes))

    def upsert_relationships(self, relationships: Any) -> int:
        if self.fail_with is not None:
            raise self.fail_with
        return len(list(relationships))

    def invalidate_relationship(self, fact_id: Any, at: Any) -> bool:
        return True

    def neighbours(self, entity_ids: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def query(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return [{"n": self.node_count}]

    def clear(self) -> None:  # pragma: no cover - must never be called by the rebuild driver
        raise AssertionError("rebuild-graph must not clear the whole database")

    def health(self) -> bool:
        return True


def test_wipe_targets_only_ontology_labels_and_never_the_whole_database() -> None:
    store = RecordingStore(node_count=661)
    ontology = load_ontology()

    removed = wipe_projection(store, ontology=ontology)

    assert removed == 661
    assert len(store.statements) == len(ontology.stored_labels)
    targeted = {s.split("`")[1] for s in store.statements}
    assert targeted == set(ontology.stored_labels)
    # NeoDash keeps its saved dashboards in the same database under its own label.
    assert not any("_Neodash" in s or "MATCH (n) DETACH" in s for s in store.statements)


def test_wipe_runs_no_statement_when_there_is_nothing_to_remove() -> None:
    store = RecordingStore(node_count=0)
    assert wipe_projection(store) == 0
    assert store.statements == []


# ====================================================================================================
# Degradation: the graph is optional, the knowledge is not (ADR-0001)
# ====================================================================================================


def test_projection_reports_a_graph_outage_instead_of_raising() -> None:
    """The writer reaches `_project` *after* PostgreSQL is written. Raising here would roll back
    knowledge that is already safely persisted, so the failure is reported and the run continues."""
    project = _entity("JobLab DE", EntityType.PROJECT)
    plan = ProjectionPlan()
    plan.nodes.append(entity_node(project))
    store = RecordingStore(fail_with=ConnectionResetError("Failed to read from defunct connection"))

    report = GraphProjector(store).apply(plan)

    assert report.ok is False
    assert report.nodes_written == 0
    assert any("ConnectionResetError" in message for message in report.errors)


def test_strict_projector_raises_because_a_failed_rebuild_did_not_happen() -> None:
    project = _entity("JobLab DE", EntityType.PROJECT)
    plan = ProjectionPlan()
    plan.nodes.append(entity_node(project))
    store = RecordingStore(fail_with=ConnectionResetError("boom"))

    with pytest.raises(ConnectionResetError):
        GraphProjector(store, strict=True).apply(plan)


def test_open_graph_store_returns_none_when_nothing_is_listening() -> None:
    """A real refused connection, not a stub: this is the outage a Tier 2 run must survive."""
    settings = get_settings().model_copy(deep=True)
    settings.neo4j.uri = DEAD_BOLT_URI

    assert open_graph_store(settings) is None


def test_open_graph_store_raises_when_the_caller_says_it_is_required() -> None:
    """`rebuild-graph` passes `required=True`: a rebuild that cannot reach Neo4j achieved nothing."""
    settings = get_settings().model_copy(deep=True)
    settings.neo4j.uri = DEAD_BOLT_URI

    with pytest.raises(Exception, match=r".*"):
        open_graph_store(settings, required=True)


def test_get_writer_still_returns_a_writer_when_neo4j_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2 keeps extracting into PostgreSQL; the graph is repaired later by rebuild-graph."""
    import aimemory.common.config as config_module
    import aimemory.knowledge.persist as persist_module

    settings = get_settings().model_copy(deep=True)
    settings.neo4j.uri = DEAD_BOLT_URI
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    captured: dict[str, Any] = {}

    def _fake_make_result_writer(scope: Any, *, graph: Any = None, **kwargs: Any) -> Any:
        captured["graph"] = graph
        return lambda result, episode: None

    monkeypatch.setattr(persist_module, "make_result_writer", _fake_make_result_writer)

    writer = get_writer(scope=object())

    assert callable(writer)
    assert captured["graph"] is None  # degraded, not crashed


def test_get_writer_opens_a_store_when_one_is_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is *not* "no projection": a healthy Neo4j must be wired in, or every Tier 2 run
    leaves the graph stale until someone remembers to rebuild it."""
    import aimemory.knowledge.persist as persist_module
    import aimemory.providers.graph.factory as factory_module

    sentinel = RecordingStore()
    monkeypatch.setattr(factory_module, "open_graph_store", lambda *a, **k: sentinel)

    captured: dict[str, Any] = {}

    def _fake_make_result_writer(scope: Any, *, graph: Any = None, **kwargs: Any) -> Any:
        captured["graph"] = graph
        return lambda result, episode: None

    monkeypatch.setattr(persist_module, "make_result_writer", _fake_make_result_writer)

    get_writer(scope=object())

    assert captured["graph"] is sentinel
