"""Building the Neo4j payloads: PostgreSQL rows -> ``GraphNode`` / ``GraphRelationship`` (A08).

``ontology.md`` §2/§3 fixes the property contract; this module is the single place that satisfies it,
so a node can never reach :class:`~aimemory.persistence.graph_store.Neo4jGraphStore` missing
``engine`` or ``observed_at`` and a relationship can never reach it missing ``fact_id``.

Three rules encoded here that are easy to get wrong:

1. **Functional predicates are not edges** (``ontology.md`` §4). ``HAS_STATUS``, ``HAS_OWNER``,
   ``USES_ARCHITECTURE``, ``DEPLOYED_ON``, ``HAS_STAGE`` and ``SELECTED_OPTION`` live in ``facts``.
   In Neo4j they appear as a **node property** when their object is a literal, and as a
   ``RELATED_TO`` edge carrying ``predicate=<the functional predicate>`` when their object is itself
   an entity. Writing them as their own relationship type would invent six edge types the ontology
   does not declare.
2. **Validate before interpolating.** :meth:`Ontology.validate_relationship` is called for every edge
   here, against the labels we are about to write, and the store calls it again against the labels
   actually in the graph. A rejected edge is *dropped with a recorded reason*, never written and
   never silently retyped.
3. **Endpoints first.** ``Neo4jGraphStore.upsert_relationships`` resolves endpoint labels by looking
   the nodes up, so both endpoints must already be written. :class:`ProjectionPlan` keeps nodes and
   edges in separate lists precisely so the caller cannot interleave them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from ...common.config import get_settings
from ...common.errors import OntologyError
from ...common.time import ensure_utc
from ...domain.enums import ArtifactType, EngineKind, EntityType, Predicate
from ...domain.models import Entity, Fact, KnowledgeArtifact
from ...domain.ports import GraphNode, GraphRelationship
from ...ontology import Ontology, load_ontology

__all__ = [
    "ARTIFACT_LABELS",
    "FUNCTIONAL_NODE_PROPERTY",
    "NodePropertyUpdate",
    "ProjectionPlan",
    "artifact_label",
    "artifact_node",
    "entity_node",
    "fact_edges",
    "registry_node",
    "supersedes_edge",
]

#: ``knowledge_artifacts.type`` -> the ontology label it is projected as. ``summary`` has no node:
#: it is the episode's own summary, already carried by the ``Episode`` / ``Document`` node.
ARTIFACT_LABELS: dict[ArtifactType, EntityType] = {
    ArtifactType.DECISION: EntityType.DECISION,
    ArtifactType.REQUIREMENT: EntityType.REQUIREMENT,
    ArtifactType.TASK: EntityType.TASK,
    ArtifactType.FINDING: EntityType.RESEARCH_FINDING,
    ArtifactType.HYPOTHESIS: EntityType.RESEARCH_FINDING,
    ArtifactType.EXPERIMENT: EntityType.EXPERIMENT,
}

#: Literal-valued functional facts become this node property (``ontology.md`` §4).
#: ADR-0015 trimmed this to the three predicates that remain functional. ``HAS_OWNER``,
#: ``USES_ARCHITECTURE`` and ``DEPLOYED_ON`` were demoted to ordinary multi-valued edges and now
#: project as ``:HAS_OWNER`` / ``:USES_ARCHITECTURE`` / ``:DEPLOYED_ON`` instead of collapsing into a
#: single node property. Keeping them here would have re-imposed, in the graph, exactly the
#: one-value-per-subject assumption the ADR removed from the relational store - MEASURED: 24 true
#: ``USES_ARCHITECTURE`` facts had been rewritten as historical under the old rule.
FUNCTIONAL_NODE_PROPERTY: dict[str, str] = {
    Predicate.HAS_STATUS.value: "status",
    Predicate.HAS_STAGE.value: "has_stage",
    Predicate.SELECTED_OPTION.value: "selected_option",
}


def artifact_label(artifact_type: ArtifactType | str) -> EntityType | None:
    return ARTIFACT_LABELS.get(ArtifactType(artifact_type))


@dataclass(slots=True)
class NodePropertyUpdate:
    """A literal-valued functional fact, projected onto the subject node instead of as an edge."""

    node_id: str
    properties: dict[str, Any]
    fact_id: str


@dataclass(slots=True)
class ProjectionPlan:
    """Nodes, then edges, then node-property updates - in the order they must be applied."""

    nodes: list[GraphNode] = field(default_factory=list)
    relationships: list[GraphRelationship] = field(default_factory=list)
    property_updates: list[NodePropertyUpdate] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)

    def extend(self, other: ProjectionPlan) -> ProjectionPlan:
        self.nodes.extend(other.nodes)
        self.relationships.extend(other.relationships)
        self.property_updates.extend(other.property_updates)
        self.dropped.extend(other.dropped)
        return self

    @property
    def counts(self) -> dict[str, int]:
        return {
            "nodes": len(self.nodes),
            "relationships": len(self.relationships),
            "property_updates": len(self.property_updates),
            "dropped": len(self.dropped),
        }


def _drop_reason(exc: OntologyError) -> str:
    """Why an edge was refused, in a form that can be grouped in a report.

    :meth:`AiMemoryError.__str__` deliberately hides ``detail`` (it can carry paths and secrets), but
    an ontology violation's detail is only label names - ``Technology -[PART_OF]-> Project`` - and
    without it every rejection reads identically and the rebuild report can say nothing useful about
    *what* the extractor produced that the ontology does not allow.
    """
    return f"{exc}" if not exc.detail else f"{exc} ({exc.detail})"


def _clean(properties: Mapping[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values: Neo4j has no null property, and ``valid_to`` absent means 'current'."""
    out: dict[str, Any] = {}
    for key, value in properties.items():
        if value is None:
            continue
        out[key] = ensure_utc(value) if isinstance(value, datetime) else value
    return out


# --------------------------------------------------------------------------------------------------
# Nodes
# --------------------------------------------------------------------------------------------------


def entity_node(
    entity: Entity,
    *,
    observed_at: datetime | None = None,
    episode_id: UUID | str | None = None,
    source_id: UUID | str | None = None,
    valid_from: datetime | None = None,
    parent_id: str | None = None,
) -> GraphNode:
    """One ``entities`` row as a node. ``type`` keeps ``SubProject``; the *label* becomes ``Project``."""
    properties = _clean(
        {
            "id": str(entity.id),
            "name": entity.canonical_name,
            "type": entity.type.value,
            "observed_at": observed_at or entity.last_seen_at,
            "engine": entity.engine.value,
            "project_id": entity.project_id,
            "summary": entity.summary,
            "status": entity.status,
            "valid_from": valid_from,
            "source_id": str(source_id) if source_id else None,
            "episode_id": str(episode_id) if episode_id else None,
            "confidence": entity.confidence,
            "parent_id": parent_id,
            "normalized_name": entity.normalized_name,
        }
    )
    return GraphNode(id=str(entity.id), label=entity.type, properties=properties)


def artifact_node(artifact: KnowledgeArtifact) -> GraphNode | None:
    """One ``knowledge_artifacts`` row as a ``Decision`` / ``Requirement`` / ... node."""
    label = artifact_label(artifact.type)
    if label is None:
        return None
    properties = _clean(
        {
            "id": str(artifact.id),
            "name": artifact.title,
            "type": label.value,
            "observed_at": artifact.provenance.observed_at,
            "engine": artifact.engine.value,
            "project_id": artifact.project_id,
            "summary": artifact.body[:600],
            "status": artifact.current_status.value,
            "valid_from": artifact.valid_from,
            "valid_to": artifact.valid_to,
            "source_id": str(artifact.provenance.source_id)
            if artifact.provenance.source_id
            else None,
            "episode_id": str(artifact.provenance.episode_id)
            if artifact.provenance.episode_id
            else None,
            "confidence": artifact.confidence,
            "artifact_type": artifact.type.value,
            "source_status": artifact.source_status.value,
        }
    )
    return GraphNode(id=str(artifact.id), label=label, properties=properties)


def registry_node(
    node_id: UUID | str,
    label: EntityType,
    name: str,
    *,
    observed_at: datetime,
    engine: EngineKind = EngineKind.DETERMINISTIC,
    **extra: Any,
) -> GraphNode:
    """A ``Device`` / ``Source`` / ``Episode`` / ``Document`` / ``Project`` node from the registry."""
    properties = _clean(
        {
            "id": str(node_id),
            "name": name,
            "type": label.value,
            "observed_at": observed_at,
            "engine": engine.value,
            **extra,
        }
    )
    return GraphNode(id=str(node_id), label=label, properties=properties)


# --------------------------------------------------------------------------------------------------
# Edges
# --------------------------------------------------------------------------------------------------


def _relationship_properties(fact: Fact, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return _clean(
        {
            "fact_id": str(fact.id),
            "valid_from": fact.valid_from,
            "valid_to": fact.valid_to,
            "observed_at": fact.observed_at,
            "engine": fact.engine.value,
            "confidence": fact.confidence,
            "episode_id": str(fact.provenance.episode_id)
            if fact.provenance.episode_id
            else None,
            "source_id": str(fact.provenance.source_id) if fact.provenance.source_id else None,
            "status": fact.status.value,
            # ADR-0005 rule 1: a functional fact that closed an older one links to it. The ontology
            # declares SUPERSEDES between artifact labels only, so at fact level the chain has to be
            # a property - without it the graph shows a closed window with nothing saying what closed
            # it, and `explain()` would be the only way to find out.
            "supersedes_fact_id": str(fact.supersedes_fact_id) if fact.supersedes_fact_id else None,
            **(extra or {}),
        }
    )


def fact_edges(
    facts: Iterable[Fact],
    *,
    labels: Mapping[str, EntityType],
    ontology: Ontology | None = None,
    allow_ontology_violations: bool | None = None,
) -> ProjectionPlan:
    """Turn stored facts into edges and node-property updates.

    ``labels`` maps entity id -> its ontology type; endpoints missing from it cannot be validated and
    are dropped with a reason rather than guessed.

    ``allow_ontology_violations`` (default: ``NEO4J__allow_ontology_violations``) keeps an edge whose
    endpoint types break the ADR-0015 contract instead of dropping it. The edge is **marked, not
    laundered**: it carries ``ontology_violation`` naming the rule it breaks, so a reader can exclude
    those with one predicate and the graph never asserts that a backwards edge is well formed.
    Endpoints that are missing entirely are still dropped either way - there is nothing to write.
    """
    onto = ontology or load_ontology()
    if allow_ontology_violations is None:
        allow_ontology_violations = get_settings().neo4j.allow_ontology_violations
    plan = ProjectionPlan()

    for fact in facts:
        predicate = str(fact.predicate)
        subject_id = str(fact.subject_entity_id)
        subject_label = labels.get(subject_id)
        if subject_label is None:
            plan.dropped.append((str(fact.id), "subject entity not projected"))
            continue

        if onto.is_functional(predicate):
            if fact.object_entity_id is None:
                prop = FUNCTIONAL_NODE_PROPERTY.get(predicate, predicate.lower())
                plan.property_updates.append(
                    NodePropertyUpdate(
                        node_id=subject_id,
                        properties=_clean(
                            {prop: fact.object_value, f"{prop}_valid_from": fact.valid_from}
                        ),
                        fact_id=str(fact.id),
                    )
                )
                continue
            # entity-valued functional fact -> RELATED_TO, tagged with the real predicate
            object_id = str(fact.object_entity_id)
            object_label = labels.get(object_id)
            if object_label is None:
                plan.dropped.append((str(fact.id), "object entity not projected"))
                continue
            try:
                onto.validate_relationship(
                    Predicate.RELATED_TO.value, subject_label.value, object_label.value
                )
                violation = None
            except OntologyError as exc:
                violation = _drop_reason(exc)
                if not allow_ontology_violations:
                    plan.dropped.append((str(fact.id), violation))
                    continue
            extra: dict[str, Any] = {"predicate": predicate}
            if violation:
                extra["ontology_violation"] = violation
            plan.relationships.append(
                GraphRelationship(
                    fact_id=str(fact.id),
                    predicate=Predicate.RELATED_TO,
                    from_id=subject_id,
                    to_id=object_id,
                    properties=_relationship_properties(fact, extra=extra),
                )
            )
            continue

        if fact.object_entity_id is None:
            plan.dropped.append(
                (str(fact.id), f"{predicate} is not functional and its object is a literal")
            )
            continue
        object_id = str(fact.object_entity_id)
        object_label = labels.get(object_id)
        if object_label is None:
            plan.dropped.append((str(fact.id), "object entity not projected"))
            continue
        try:
            onto.validate_relationship(predicate, subject_label.value, object_label.value)
            violation = None
        except OntologyError as exc:
            violation = _drop_reason(exc)
            if not allow_ontology_violations:
                plan.dropped.append((str(fact.id), violation))
                continue
        plan.relationships.append(
            GraphRelationship(
                fact_id=str(fact.id),
                predicate=Predicate(predicate),
                from_id=subject_id,
                to_id=object_id,
                properties=_relationship_properties(
                    fact, extra={"ontology_violation": violation} if violation else None
                ),
            )
        )

    return plan


def supersedes_edge(
    new_artifact: KnowledgeArtifact,
    old_artifact: KnowledgeArtifact,
    *,
    ontology: Ontology | None = None,
) -> GraphRelationship | None:
    """``(new)-[:SUPERSEDES]->(old)`` (ADR-0005 rule 6). ``fact_id`` is the new artifact's id.

    ``SUPERSEDES`` is declared between ``Decision``/``Requirement``/``Task``/``ResearchFinding``
    only, so a superseded ``Experiment`` keeps its Postgres chain and gets no edge - reported by the
    caller rather than written as something else.
    """
    onto = ontology or load_ontology()
    new_label = artifact_label(new_artifact.type)
    old_label = artifact_label(old_artifact.type)
    if new_label is None or old_label is None:
        return None
    try:
        onto.validate_relationship(Predicate.SUPERSEDES.value, new_label.value, old_label.value)
    except OntologyError:
        return None
    return GraphRelationship(
        fact_id=str(new_artifact.id),
        predicate=Predicate.SUPERSEDES,
        from_id=str(new_artifact.id),
        to_id=str(old_artifact.id),
        properties=_clean(
            {
                "fact_id": str(new_artifact.id),
                "valid_from": new_artifact.valid_from,
                "observed_at": new_artifact.provenance.observed_at,
                "engine": new_artifact.engine.value,
                "confidence": new_artifact.confidence,
                "episode_id": str(new_artifact.provenance.episode_id)
                if new_artifact.provenance.episode_id
                else None,
            }
        ),
    )


def structural_edge(
    predicate: Predicate | str,
    *,
    from_id: UUID | str,
    to_id: UUID | str,
    from_label: EntityType,
    to_label: EntityType,
    observed_at: datetime,
    edge_id: UUID | str,
    ontology: Ontology | None = None,
    **extra: Any,
) -> GraphRelationship | None:
    """A deterministic edge (``engine='deterministic'``). ``edge_id`` becomes its ``fact_id`` key."""
    onto = ontology or load_ontology()
    name = predicate.value if isinstance(predicate, Predicate) else str(predicate)
    try:
        onto.validate_relationship(name, from_label.value, to_label.value)
    except OntologyError:
        return None
    return GraphRelationship(
        fact_id=str(edge_id),
        predicate=Predicate(name),
        from_id=str(from_id),
        to_id=str(to_id),
        properties=_clean(
            {
                "fact_id": str(edge_id),
                "valid_from": observed_at,
                "observed_at": observed_at,
                "engine": EngineKind.DETERMINISTIC.value,
                **extra,
            }
        ),
    )


def node_ids(nodes: Sequence[GraphNode]) -> set[str]:
    return {str(node.id) for node in nodes}
