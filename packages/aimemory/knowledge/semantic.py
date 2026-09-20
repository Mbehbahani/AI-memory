"""The semantic graph: what the extraction engine found, replayed from PostgreSQL (owner A08).

The structural layer (:mod:`aimemory.knowledge.structural`) is true by construction. This layer is
the other half of the projection - ``entities``, ``facts`` and ``knowledge_artifacts``, everything
ADR-0009's native engine wrote - and it exists so that a projection can be rebuilt *from the system
of record* rather than only being produced as a side effect of extraction (ADR-0001).

============================  =================================================================
``entities``                  ``(:Project|:Technology|...:Entity)``
``knowledge_artifacts``       ``(:Decision|:Requirement|:Task|:ResearchFinding|:Experiment)``
``facts``                     an edge, or a node property for a literal-valued functional fact
``knowledge_artifacts`` chain ``SUPERSEDES``, plus ``DERIVED_FROM`` / ``DECIDED_IN`` to the episode
============================  =================================================================

Deliberately **mirrors** :meth:`aimemory.knowledge.persist.KnowledgeWriter._project` edge for edge.
The incremental path (write an episode, project it) and the rebuild path (replay everything) have to
produce the same graph, otherwise "rebuild-graph reproduces identical counts" is meaningless. Any
edge added to one belongs in the other.

Two things are *not* hidden when they happen, because both are real findings rather than noise:

* a fact whose subject or object entity is not projected (merged away, or filtered out by
  ``project_id``) is dropped with that reason;
* a fact whose endpoint labels the ontology does not allow for its predicate - e.g. the extractor's
  ``PART_OF`` from a ``Technology``, which ``schemas/ontology.yaml`` permits only from
  ``SubProject``/``Document``/``Task``/``Requirement`` - is dropped by
  :func:`~aimemory.providers.graph.projection.fact_edges` with the ontology's own message. It stays
  in PostgreSQL either way; the graph is the constrained view, not the record.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.logging import get_logger
from ..domain.enums import ArtifactType, EntityType, Predicate
from ..domain.models import Entity, Fact, KnowledgeArtifact
from ..ontology import Ontology, load_ontology
from ..persistence.repositories import _artifact_from_row, _fact_from_row  # noqa: PLC2701
from ..providers.graph import (
    ProjectionPlan,
    artifact_label,
    artifact_node,
    entity_node,
    fact_edges,
    structural_edge,
    supersedes_edge,
)

__all__ = ["SemanticIndex", "semantic_plan"]

logger = get_logger(__name__)

#: Artifact labels that carry ``DERIVED_FROM`` back to the episode they were extracted from.
#: Same tuple as ``persist.py``'s ``_DERIVED_FROM_LABELS`` - the ontology declares no
#: ``DERIVED_FROM`` from ``Experiment``.
_DERIVED_FROM_LABELS = frozenset(
    {
        EntityType.DECISION,
        EntityType.REQUIREMENT,
        EntityType.TASK,
        EntityType.RESEARCH_FINDING,
        EntityType.CONCEPT,
    }
)


class SemanticIndex:
    """What the semantic pass learned, for the rebuild driver's report."""

    __slots__ = ("artifacts", "entities", "facts", "labels", "skipped_summaries")

    def __init__(self) -> None:
        self.labels: dict[str, EntityType] = {}
        self.entities: int = 0
        self.artifacts: int = 0
        self.facts: int = 0
        self.skipped_summaries: int = 0


def semantic_plan(
    session: Session,
    *,
    ontology: Ontology | None = None,
    project_id: str | None = None,
    known_labels: Mapping[str, EntityType] | None = None,
) -> tuple[ProjectionPlan, SemanticIndex]:
    """Build the engine-produced layer. Pure read; nothing is written here.

    ``known_labels`` are the nodes the structural pass already placed (episodes above all) - an
    artifact's ``DERIVED_FROM`` edge is only emitted when its episode is actually in the graph.
    """
    onto = ontology or load_ontology()
    plan = ProjectionPlan()
    index = SemanticIndex()
    placed: dict[str, EntityType] = dict(known_labels or {})

    entities = _entities(session, project_id)
    for entity in entities:
        plan.nodes.append(entity_node(entity))
        index.labels[str(entity.id)] = entity.type
        placed[str(entity.id)] = entity.type
    index.entities = len(entities)

    artifacts = _artifacts(session, project_id)
    by_id = {str(a.id): a for a in artifacts}
    for artifact in artifacts:
        node = artifact_node(artifact)
        if node is None:  # `summary` has no node of its own (ARTIFACT_LABELS)
            index.skipped_summaries += 1
            continue
        label = artifact_label(artifact.type)
        assert label is not None  # noqa: S101 - artifact_node returned a node, so the label exists
        plan.nodes.append(node)
        index.labels[str(artifact.id)] = label
        placed[str(artifact.id)] = label
        index.artifacts += 1

    facts = _facts(session, project_id)
    index.facts = len(facts)
    plan.extend(fact_edges(facts, labels=placed, ontology=onto))

    _artifact_edges(plan, artifacts, by_id, placed, onto)

    logger.info(
        "semantic.plan_built",
        entities=index.entities,
        artifacts=index.artifacts,
        facts=index.facts,
        **plan.counts,
    )
    return plan, index


# --------------------------------------------------------------------------------------------------
# Readers
# --------------------------------------------------------------------------------------------------


def _entities(session: Session, project_id: str | None) -> list[Entity]:
    """Live entities only: a row with ``merged_into_id`` set lost a merge and its edges were
    rewritten onto the winner (ontology.md §7). It stays in PostgreSQL so old provenance resolves."""
    rows = session.execute(
        text(
            """
            SELECT * FROM entities
             WHERE merged_into_id IS NULL
               AND (CAST(:pid AS text) IS NULL OR project_id = :pid)
             ORDER BY first_seen_at, id
            """
        ),
        {"pid": project_id},
    ).all()
    return [Entity(**dict(row._mapping)) for row in rows]


def _artifacts(session: Session, project_id: str | None) -> list[KnowledgeArtifact]:
    """Every artifact, superseded ones included - ADR-0005 rule 6: supersession is a state, not a
    deletion, so the old node stays in the graph carrying ``valid_to`` and ``status=superseded``."""
    rows = session.execute(
        text(
            """
            SELECT * FROM knowledge_artifacts
             WHERE (CAST(:pid AS text) IS NULL OR project_id = :pid)
             ORDER BY valid_from, id
            """
        ),
        {"pid": project_id},
    ).mappings().all()
    return [_artifact_from_row(dict(row)) for row in rows]


def _facts(session: Session, project_id: str | None) -> list[Fact]:
    """Every fact, whatever its status. ``historical`` facts keep their closed window
    (``valid_to``), ``unconfirmed`` ones keep ``valid_to IS NULL`` and their flag (ADR-0005 rules
    1/3/5), so an ``as_of`` query over the projection sees the same history PostgreSQL does."""
    rows = session.execute(
        text(
            """
            SELECT * FROM facts
             WHERE (CAST(:pid AS text) IS NULL OR project_id = :pid)
             ORDER BY valid_from, id
            """
        ),
        {"pid": project_id},
    ).mappings().all()
    return [_fact_from_row(dict(row)) for row in rows]


# --------------------------------------------------------------------------------------------------
# Artifact edges (mirrors persist.KnowledgeWriter._project)
# --------------------------------------------------------------------------------------------------


def _artifact_edges(
    plan: ProjectionPlan,
    artifacts: list[KnowledgeArtifact],
    by_id: Mapping[str, KnowledgeArtifact],
    placed: Mapping[str, EntityType],
    onto: Ontology,
) -> None:
    for artifact in artifacts:
        label = artifact_label(artifact.type)
        if label is None:
            continue
        episode_id = artifact.provenance.episode_id
        if episode_id is not None and str(episode_id) in placed:
            if label in _DERIVED_FROM_LABELS:
                edge = structural_edge(
                    Predicate.DERIVED_FROM,
                    from_id=artifact.id,
                    to_id=episode_id,
                    from_label=label,
                    to_label=EntityType.EPISODE,
                    observed_at=artifact.provenance.observed_at,
                    edge_id=f"derived_from:{artifact.id}",
                    ontology=onto,
                )
                if edge is not None:
                    plan.relationships.append(edge)
            if label is EntityType.DECISION:
                edge = structural_edge(
                    Predicate.DECIDED_IN,
                    from_id=artifact.id,
                    to_id=episode_id,
                    from_label=label,
                    to_label=EntityType.EPISODE,
                    observed_at=artifact.provenance.observed_at,
                    edge_id=f"decided_in:{artifact.id}",
                    ontology=onto,
                )
                if edge is not None:
                    plan.relationships.append(edge)

        old = by_id.get(str(artifact.supersedes_id)) if artifact.supersedes_id else None
        if old is None:
            continue
        edge_obj = supersedes_edge(artifact, old, ontology=onto)
        if edge_obj is not None:
            plan.relationships.append(edge_obj)
        else:
            plan.dropped.append(
                (
                    str(artifact.id),
                    (
                        f"SUPERSEDES {artifact.type.value} -> {old.type.value} is not an ontology "
                        "edge; the chain stays in PostgreSQL"
                    ),
                )
            )


def artifact_types_without_nodes() -> tuple[ArtifactType, ...]:
    """Artifact types the ontology gives no label to - documented, not silently skipped."""
    return tuple(t for t in ArtifactType if artifact_label(t) is None)
