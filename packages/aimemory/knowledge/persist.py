"""Persisting one :class:`ExtractionResult` - the whole Tier 2 write path (A08, P8-T02).

Plan section L, stages ``resolve entities -> temporal rules -> Postgres -> Neo4j projection ->
provenance complete``, in that order and in that order only. The order is the design:

* **PostgreSQL first, always** (ADR-0001). If the Neo4j write fails, the knowledge is already safe and
  ``rebuild-graph`` replays the projection later. The reverse would put a fact in a rebuildable
  cache and nowhere else.
* **Entity resolution before the temporal rules**, because ``apply_fact`` compares ``object_key``,
  and two spellings of one entity would look like a contradiction and close a fact that was never
  contradicted.
* **Close before insert** inside one transaction for functional predicates, because
  ``uq_facts_functional_current`` rejects two open functional facts for one subject.

Everything written here carries the full plan section J stamp, minted once per episode by
:class:`~aimemory.provenance.EpisodeProvenance`, including ``extraction_model_id`` - which ADR-0014
made load-bearing: the corpus guard can only be true if every row says which model produced it, and a
re-extraction can only supersede a previous generation if the two are distinguishable.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.ids import new_id, normalize_name
from ..common.logging import get_logger
from ..common.time import ensure_utc, parse_timestamp, utc_now, valid_from_for
from ..domain.enums import (
    ArtifactStatus,
    ArtifactType,
    EngineKind,
    EntityType,
    EpisodeType,
    Predicate,
)
from ..domain.extraction import ExtractedArtifact, ExtractedFact, ExtractionResult
from ..domain.models import Entity, EntityMention, Episode, Fact, KnowledgeArtifact
from ..ontology import Ontology, load_ontology
from ..persistence.repositories import EntityRepo
from ..providers.graph import (
    GraphProjector,
    ProjectionPlan,
    artifact_label,
    artifact_node,
    entity_node,
    fact_edges,
    registry_node,
    structural_edge,
    supersedes_edge,
)
from ..provenance import EpisodeProvenance, missing_provenance_columns
from .entity_resolution import AliasIndex, EntityResolver, ResolvedEntity, SqlEntityStore
from .temporal import (
    SqlArtifactStore,
    SqlFactStore,
    TemporalOutcome,
    apply_artifact,
    apply_fact,
    reconcile_version,
)

__all__ = [
    "KnowledgeWriter",
    "PersistReport",
    "make_result_writer",
]

logger = get_logger(__name__)

#: Artifact label -> the edge that attaches it to its project. ``BELONGS_TO`` and ``PART_OF`` have
#: different allowed endpoints in ``schemas/ontology.yaml``; ``ResearchFinding`` has neither, so it
#: gets no project edge rather than an invented one.
_PROJECT_EDGE: dict[EntityType, Predicate] = {
    EntityType.DECISION: Predicate.BELONGS_TO,
    EntityType.EXPERIMENT: Predicate.BELONGS_TO,
    EntityType.TASK: Predicate.PART_OF,
    EntityType.REQUIREMENT: Predicate.PART_OF,
}

#: Artifact labels that may carry ``DERIVED_FROM -> Episode`` (ontology.yaml line 40).
_DERIVED_FROM_LABELS = frozenset(
    {
        EntityType.DECISION,
        EntityType.REQUIREMENT,
        EntityType.TASK,
        EntityType.RESEARCH_FINDING,
        EntityType.CONCEPT,
    }
)


@dataclass(slots=True)
class PersistReport:
    """MEASURED counts of one persisted episode."""

    episode_id: UUID
    entities: int = 0
    entities_created: int = 0
    types_overridden: int = 0
    mentions: int = 0
    artifacts: int = 0
    artifacts_superseded: int = 0
    facts: int = 0
    facts_superseded: int = 0
    facts_reconfirmed: int = 0
    facts_dropped: int = 0
    graph_nodes: int = 0
    graph_relationships: int = 0
    provenance_incomplete: int = 0
    warnings: list[str] = field(default_factory=list)
    resolution_methods: dict[str, int] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "entities": self.entities,
            "entities_created": self.entities_created,
            "types_overridden": self.types_overridden,
            "mentions": self.mentions,
            "artifacts": self.artifacts,
            "artifacts_superseded": self.artifacts_superseded,
            "facts": self.facts,
            "facts_superseded": self.facts_superseded,
            "facts_reconfirmed": self.facts_reconfirmed,
            "facts_dropped": self.facts_dropped,
            "graph_nodes": self.graph_nodes,
            "graph_relationships": self.graph_relationships,
            "provenance_incomplete": self.provenance_incomplete,
        }


class KnowledgeWriter:
    """Writes one :class:`ExtractionResult`. One instance per session; never commits."""

    def __init__(
        self,
        session: Session,
        *,
        device_id: str,
        graph: Any = None,
        ontology: Ontology | None = None,
        aliases: AliasIndex | None = None,
        engine: EngineKind = EngineKind.NATIVE,
        write_summary_artifact: bool = True,
    ) -> None:
        self._s = session
        self._device_id = device_id
        self._ontology = ontology or load_ontology()
        self._graph = graph
        self._engine = engine
        self._aliases = aliases
        self._write_summary = write_summary_artifact
        self._entity_store = SqlEntityStore(session)
        self._fact_store = SqlFactStore(session)
        self._artifact_store = SqlArtifactStore(session)

    # ---- entry point ------------------------------------------------------------------------

    def write(self, result: ExtractionResult, episode: Episode) -> PersistReport:
        report = PersistReport(episode_id=episode.id)
        if not result.valid:
            report.warnings.append("result was not valid; nothing persisted")
            return report

        provenance = self._episode_provenance(episode, result)
        self._ensure_model_row(result.extraction_model_id)

        resolver = EntityResolver(
            self._entity_store,
            session=self._s,
            aliases=self._aliases,
            project_id=provenance.project_id,
            engine=self._engine,
        )
        resolved = self._resolve_entities(result, resolver, provenance, report)
        report.resolution_methods = dict(resolver.stats)

        artifacts = self._write_artifacts(result, episode, provenance, resolved, report)
        facts = self._write_facts(result, episode, provenance, resolved, report)

        self._project(episode, provenance, resolved, artifacts, facts, report)

        logger.info("knowledge.persisted", episode_id=str(episode.id), **report.counts)
        return report

    # ---- provenance -------------------------------------------------------------------------

    def _episode_provenance(
        self, episode: Episode, result: ExtractionResult
    ) -> EpisodeProvenance:
        """One stamp per episode, assembled from the source/version rows the episode points at."""
        row = self._s.execute(
            text(
                """
                SELECT s.id AS source_id, s.uri AS source_uri, s.project_id AS source_project_id,
                       v.id AS version_id, v.content_hash AS source_hash
                  FROM episodes e
                  LEFT JOIN sources s ON s.id = e.source_id
                  LEFT JOIN source_versions v ON v.id = e.version_id
                 WHERE e.id = :id
                """
            ),
            {"id": str(episode.id)},
        ).mappings().first()
        data = dict(row) if row is not None else {}
        return EpisodeProvenance(
            device_id=self._device_id,
            observed_at=ensure_utc(episode.observed_at),
            episode_id=episode.id,
            project_id=episode.project_id or data.get("source_project_id"),
            source_id=episode.source_id or data.get("source_id"),
            source_uri=data.get("source_uri"),
            source_hash=data.get("source_hash"),
            source_version=episode.version_id or data.get("version_id"),
            ingestion_run_id=episode.ingestion_run_id,
            extraction_model_id=result.extraction_model_id,
        )

    def _ensure_model_row(self, model_id: str | None) -> None:
        """``facts.extraction_model_id`` is a foreign key; a missing row would fail the insert."""
        if not model_id:
            return
        self._s.execute(
            text(
                """
                INSERT INTO extraction_models (id, provider, name, digest, parameters)
                VALUES (:id, :provider, :name, NULL, '{}'::jsonb)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": model_id,
                "provider": model_id.split(":", 1)[0] if ":" in model_id else "unknown",
                "name": model_id,
            },
        )

    # ---- entities ---------------------------------------------------------------------------

    def _resolve_entities(
        self,
        result: ExtractionResult,
        resolver: EntityResolver,
        provenance: EpisodeProvenance,
        report: PersistReport,
    ) -> dict[str, ResolvedEntity]:
        resolved: dict[str, ResolvedEntity] = {}
        entity_repo = EntityRepo(self._s)
        for extracted in result.entities:
            hit = resolver.resolve(
                extracted.name,
                extracted.type,
                aliases=list(extracted.aliases),
                summary=extracted.description,
                observed_at=provenance.observed_at,
            )
            if hit is None:
                continue
            report.entities += 1
            report.entities_created += int(hit.created)
            report.types_overridden += int(hit.type_overridden)
            report.warnings.extend(hit.warnings)
            for key in {
                normalize_name(extracted.name),
                normalize_name(hit.entity.canonical_name),
                *[normalize_name(a) for a in extracted.aliases],
            }:
                if key:
                    resolved.setdefault(key, hit)

            mention_prov = provenance.mention(confidence=hit.entity.confidence)
            entity_repo.add_mention(
                EntityMention(
                    id=new_id(),
                    entity_id=hit.entity.id,
                    episode_id=provenance.episode_id,  # type: ignore[arg-type]
                    surface_form=extracted.name[:200],
                    confidence=hit.entity.confidence,
                    provenance=mention_prov,
                )
            )
            report.mentions += 1
            report.provenance_incomplete += int(bool(missing_provenance_columns(mention_prov)))
        return resolved

    # ---- artifacts --------------------------------------------------------------------------

    def _write_artifacts(
        self,
        result: ExtractionResult,
        episode: Episode,
        provenance: EpisodeProvenance,
        resolved: dict[str, ResolvedEntity],
        report: PersistReport,
    ) -> list[tuple[KnowledgeArtifact, KnowledgeArtifact | None]]:
        written: list[tuple[KnowledgeArtifact, KnowledgeArtifact | None]] = []

        for extracted in result.artifacts:
            artifact = self._build_artifact(extracted, provenance)
            links = self._artifact_links(extracted, resolved)
            outcome = apply_artifact(
                artifact,
                store=self._artifact_store,
                supersedes_title=extracted.supersedes_if_stated,
                entity_links=links,
            )
            report.artifacts += 1
            report.warnings.extend(outcome.warnings)
            report.provenance_incomplete += int(
                bool(missing_provenance_columns(outcome.artifact.provenance))
            )
            old: KnowledgeArtifact | None = None
            if outcome.superseded_id is not None:
                report.artifacts_superseded += 1
                old = self._artifact_store.get(outcome.superseded_id)
            written.append((outcome.artifact, old))

        if self._write_summary and result.summary:
            summary = KnowledgeArtifact(
                id=new_id(),
                type=ArtifactType.SUMMARY,
                title=(episode.title or "Episode summary")[:160],
                body=result.summary,
                structured={"doc_kind": result.doc_kind.value if result.doc_kind else "other"},
                project_id=provenance.project_id,
                current_status=ArtifactStatus.CURRENT,
                valid_from=provenance.observed_at,
                engine=self._engine,
                provenance=provenance.artifact(valid_from=provenance.observed_at),
            )
            self._artifact_store.insert(summary)
            report.artifacts += 1

        return written

    def _build_artifact(
        self, extracted: ExtractedArtifact, provenance: EpisodeProvenance
    ) -> KnowledgeArtifact:
        stated = parse_timestamp(extracted.date_if_stated) if extracted.date_if_stated else None
        valid_from = valid_from_for(stated, provenance.observed_at)
        status = (
            extracted.status.to_artifact_status()
            if extracted.status is not None
            else ArtifactStatus.CURRENT
        )
        confidence = 0.6 if extracted.status is None else 1.0
        return KnowledgeArtifact(
            id=new_id(),
            type=extracted.artifact_type,
            title=extracted.title[:160],
            body=extracted.statement,
            structured={
                "stated_status": extracted.status.value if extracted.status else "unknown",
                "related_entities": list(extracted.related_entities),
            },
            project_id=provenance.project_id,
            current_status=status,
            valid_from=valid_from,
            confidence=confidence,
            evidence_quote=extracted.evidence_quote,
            engine=self._engine,
            provenance=provenance.artifact(valid_from=valid_from, confidence=confidence),
        )

    def _artifact_links(
        self, extracted: ExtractedArtifact, resolved: dict[str, ResolvedEntity]
    ) -> list[tuple[UUID, str]]:
        links: list[tuple[UUID, str]] = []
        seen: set[UUID] = set()
        for name in extracted.related_entities:
            hit = resolved.get(normalize_name(name))
            if hit is not None and hit.entity.id not in seen:
                seen.add(hit.entity.id)
                links.append((hit.entity.id, "about"))
        return links

    # ---- facts ------------------------------------------------------------------------------

    def _write_facts(
        self,
        result: ExtractionResult,
        episode: Episode,
        provenance: EpisodeProvenance,
        resolved: dict[str, ResolvedEntity],
        report: PersistReport,
    ) -> list[Fact]:
        stored: list[Fact] = []
        for extracted in result.facts:
            fact = self._build_fact(extracted, provenance, resolved)
            if fact is None:
                report.facts_dropped += 1
                continue
            outcome: TemporalOutcome = apply_fact(
                fact,
                store=self._fact_store,
                episode_id=episode.id,
                ontology=self._ontology,
                graph=self._graph,
            )
            report.warnings.extend(outcome.warnings)
            report.facts += 1
            if outcome.action.value == "superseded":
                report.facts_superseded += 1
            if outcome.action.value == "reconfirmed":
                report.facts_reconfirmed += 1
            report.provenance_incomplete += int(
                bool(missing_provenance_columns(outcome.fact.provenance))
            )
            stored.append(outcome.fact)
        return stored

    def _build_fact(
        self,
        extracted: ExtractedFact,
        provenance: EpisodeProvenance,
        resolved: dict[str, ResolvedEntity],
    ) -> Fact | None:
        subject = resolved.get(normalize_name(extracted.subject))
        if subject is None:
            return None
        obj = resolved.get(normalize_name(extracted.object))
        functional = self._ontology.is_functional(extracted.predicate)
        if obj is None and not functional:
            # ExtractedFact's contract: unresolvable triples are dropped, never guessed.
            return None

        stated = (
            parse_timestamp(extracted.valid_from_if_stated)
            if extracted.valid_from_if_stated
            else None
        )
        valid_from = valid_from_for(stated, provenance.observed_at)
        valid_to = (
            parse_timestamp(extracted.valid_to_if_stated)
            if extracted.valid_to_if_stated
            else None
        )
        confidence = extracted.confidence if extracted.confidence is not None else 0.8
        return Fact(
            id=new_id(),
            subject_entity_id=subject.entity.id,
            predicate=Predicate(extracted.predicate),
            object_entity_id=obj.entity.id if obj is not None else None,
            object_value=None if obj is not None else extracted.object[:200],
            statement=extracted.statement,
            valid_from=valid_from,
            valid_to=valid_to if valid_to and valid_to > valid_from else None,
            observed_at=provenance.observed_at,
            confidence=confidence,
            engine=self._engine,
            project_id=provenance.project_id,
            provenance=provenance.fact(valid_from=valid_from, confidence=confidence),
        )

    # ---- Neo4j projection -------------------------------------------------------------------

    def _project(
        self,
        episode: Episode,
        provenance: EpisodeProvenance,
        resolved: dict[str, ResolvedEntity],
        artifacts: Sequence[tuple[KnowledgeArtifact, KnowledgeArtifact | None]],
        facts: Sequence[Fact],
        report: PersistReport,
    ) -> None:
        if self._graph is None:
            return
        plan = ProjectionPlan()
        labels: dict[str, EntityType] = {}

        entities = {hit.entity.id: hit.entity for hit in resolved.values()}
        for entity in entities.values():
            plan.nodes.append(
                entity_node(
                    entity,
                    observed_at=provenance.observed_at,
                    episode_id=provenance.episode_id,
                    source_id=provenance.source_id,
                )
            )
            labels[str(entity.id)] = entity.type

        episode_node_obj = registry_node(
            episode.id,
            EntityType.EPISODE,
            episode.title or str(episode.type),
            observed_at=provenance.observed_at,
            engine=self._engine,
            project_id=provenance.project_id,
            source_id=str(provenance.source_id) if provenance.source_id else None,
            episode_type=str(episode.type),
        )
        plan.nodes.append(episode_node_obj)
        labels[str(episode.id)] = EntityType.EPISODE

        for artifact, old in artifacts:
            node = artifact_node(artifact)
            if node is None:
                continue
            plan.nodes.append(node)
            label = artifact_label(artifact.type)
            assert label is not None  # noqa: S101 - artifact_node returned a node, so the label exists
            labels[str(artifact.id)] = label
            if old is not None:
                old_node = artifact_node(old)
                if old_node is not None:
                    plan.nodes.append(old_node)
                    old_label = artifact_label(old.type)
                    if old_label is not None:
                        labels[str(old.id)] = old_label

        plan.extend(fact_edges(facts, labels=labels, ontology=self._ontology))

        # MENTIONS: Episode -> every entity this episode mentioned (structural, deterministic).
        for entity in entities.values():
            edge = structural_edge(
                Predicate.MENTIONS,
                from_id=episode.id,
                to_id=entity.id,
                from_label=EntityType.EPISODE,
                to_label=entity.type,
                observed_at=provenance.observed_at,
                edge_id=f"mentions:{episode.id}:{entity.id}",
                ontology=self._ontology,
            )
            if edge is not None:
                plan.relationships.append(edge)

        for artifact, old in artifacts:
            label = artifact_label(artifact.type)
            if label is None:
                continue
            if label in _DERIVED_FROM_LABELS:
                edge = structural_edge(
                    Predicate.DERIVED_FROM,
                    from_id=artifact.id,
                    to_id=episode.id,
                    from_label=label,
                    to_label=EntityType.EPISODE,
                    observed_at=provenance.observed_at,
                    edge_id=f"derived_from:{artifact.id}",
                    ontology=self._ontology,
                )
                if edge is not None:
                    plan.relationships.append(edge)
            if label is EntityType.DECISION:
                edge = structural_edge(
                    Predicate.DECIDED_IN,
                    from_id=artifact.id,
                    to_id=episode.id,
                    from_label=label,
                    to_label=EntityType.EPISODE,
                    observed_at=provenance.observed_at,
                    edge_id=f"decided_in:{artifact.id}",
                    ontology=self._ontology,
                )
                if edge is not None:
                    plan.relationships.append(edge)
            if old is not None:
                edge_obj = supersedes_edge(artifact, old, ontology=self._ontology)
                if edge_obj is not None:
                    plan.relationships.append(edge_obj)
                    self._close_old_edges(old)

        report_projection = GraphProjector(self._graph).apply(plan)
        report.graph_nodes = report_projection.nodes_written
        report.graph_relationships = report_projection.relationships_written
        report.warnings.extend(report_projection.errors)
        report.warnings.extend(f"dropped edge {fid}: {why}" for fid, why in report_projection.dropped)

    def _close_old_edges(self, old: KnowledgeArtifact) -> None:
        """A superseded artifact's own edge carries ``valid_to``; the edge is never deleted."""
        if old.valid_to is None:
            return
        try:
            self._graph.invalidate_relationship(old.id, old.valid_to)
        except Exception as exc:  # noqa: BLE001 - ADR-0001: never fail a persisted write on the graph
            logger.warning("graph.close_edge_failed", error=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------------------------------
# Wiring for A07a's Tier 2 runner
# --------------------------------------------------------------------------------------------------


def make_result_writer(
    scope: Any,
    *,
    device_id: str | None = None,
    graph: Any = None,
    engine: EngineKind = EngineKind.NATIVE,
    reports: list[PersistReport] | None = None,
) -> Callable[[ExtractionResult, Episode], None]:
    """The ``ResultWriter`` callable :func:`aimemory.sources.tier2.run_tier2` expects.

    ``run_tier2`` calls ``writer(result, episode)`` without handing over a session, so the closure
    opens its own unit of work per episode - which is also the transaction boundary the functional
    close-then-insert pair needs.
    """
    from ..common.config import get_settings  # noqa: PLC0415 - avoid an import cycle

    resolved_device = device_id or get_settings().device_id

    def _write(result: ExtractionResult, episode: Episode) -> None:
        with scope.session() as session:
            writer = KnowledgeWriter(
                session, device_id=resolved_device, graph=graph, engine=engine
            )
            report = writer.write(result, episode)
            if reports is not None:
                reports.append(report)

    return _write


def reconcile_after_change(
    session: Session,
    *,
    old_version_id: UUID,
    new_facts: Sequence[Fact],
    episode_id: UUID | None = None,
    ontology: Ontology | None = None,
) -> dict[str, int]:
    """ADR-0005 rule 3 after a ``document_change`` episode has been persisted.

    Called by the ingestion path once the new version's facts exist, so ``apply_fact`` has already
    reconfirmed what was re-stated and closed what was contradicted; everything left over becomes
    ``unconfirmed``.
    """
    report = reconcile_version(
        old_version_id=old_version_id,
        reextracted=list(new_facts),
        store=SqlFactStore(session),
        episode_id=episode_id,
        ontology=ontology,
    )
    return report.counts
