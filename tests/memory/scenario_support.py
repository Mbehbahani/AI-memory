"""Scenario support for the memory suite: a deterministic knowledge engine and its writer (A07a).

The eleven scenarios of plan sections L and Y split into two groups:

* **1-7 (change detection)** are properties of the ingestion state machine and run against the real
  pipeline - real walk, real hashes, real Postgres, real embeddings.
* **8-11 (conflicting fact, superseded decision, timeline, provenance)** are properties of what the
  *knowledge* side does with what ingestion hands it. They need facts and artifacts, and ingestion
  writes neither by itself (ADR-0001: the engine returns a result, the caller persists it).

So this module supplies the seam: a deterministic :class:`StubKnowledgeEngine` that satisfies
``aimemory.domain.ports.KnowledgeEngine`` and returns a fixed ``ExtractionResult`` per fixture
document, plus :func:`persist_result` - a minimal writer built on A04's repositories and the ADR-0005
primitives they expose (``find_open_fact`` / ``close_fact`` / ``insert_superseding`` /
``ArtifactRepo.supersede``).

**What this is and is not.** It is not a reimplementation of A08's engine and makes no claim about
extraction quality: the stub's "extraction" is a hard-coded table, so nothing here can pass because a
model got lucky. What it exercises is the half A07a owns and A08 depends on: that the Tier 2 queue
claims episodes in priority order, that a suspected secret never reaches a provider, that the
ADR-0014 one-model-per-corpus guard fires before any egress, that the entity-type seed guard is
enforced at write time, and that every row written from an episode can be walked back to a source
version (``provenance_v``). When A08's native engine lands it replaces ``StubKnowledgeEngine`` in
these tests without the assertions changing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from aimemory.common.ids import new_id, normalize_name
from aimemory.domain.enums import (
    ArtifactStatus,
    ArtifactType,
    EngineKind,
    EntityType,
    FactStatus,
    Predicate,
    SourceStatus,
)
from aimemory.domain.extraction import (
    ExtractedArtifact,
    ExtractedEntity,
    ExtractedFact,
    ExtractionResult,
)
from aimemory.domain.models import Entity, Episode, ExtractionModel, Fact, KnowledgeArtifact
from aimemory.domain.provenance import Provenance
from aimemory.persistence.repositories import ArtifactRepo, EntityRepo, FactRepo
from aimemory.sources.seeds import resolve_entity_type
from sqlalchemy.orm import Session

__all__ = [
    "StubKnowledgeEngine",
    "persist_result",
]

#: The model id the stub claims to be. A real ``extraction_models`` row is created for it by
#: ``run_tier2`` before anything is stamped with it (the column is a foreign key).
STUB_MODEL_ID = "stub:scenario-v1"
#: A second, deliberately different model id - the ADR-0014 mismatch test needs two.
OTHER_MODEL_ID = "stub:other-model-v1"


# ==================================================================================================
# A deterministic KnowledgeEngine
# ==================================================================================================


@dataclass(frozen=True)
class _Scripted:
    """What the stub "extracts" from one fixture document."""

    entities: tuple[tuple[str, EntityType], ...] = ()
    artifacts: tuple[ExtractedArtifact, ...] = ()
    facts: tuple[ExtractedFact, ...] = ()


def _decision(
    title: str, statement: str, date: str, supersedes: str | None = None
) -> ExtractedArtifact:
    return ExtractedArtifact(
        artifact_type=ArtifactType.DECISION,
        title=title,
        statement=statement,
        date_if_stated=date,
        supersedes_if_stated=supersedes,
        evidence_quote=statement[:200],
    )


#: Keyed by the episode title (the file name), because that is what the queue hands the engine.
#: Mirrors ``tests/fixtures/mini-vault/README.md``: A selected 2026-09-01, abandoned 2026-09-10,
#: B selected 2026-09-11, B supersedes A.
SCRIPT: dict[str, _Scripted] = {
    "architecture-decision-a.md": _Scripted(
        entities=(
            ("Fixture Project", EntityType.PROJECT),
            ("Architecture A", EntityType.CONCEPT),
            ("Neo4j", EntityType.TECHNOLOGY),
        ),
        artifacts=(
            _decision(
                "Architecture A selected",
                "Architecture A (a monolithic ingestion worker writing directly to Neo4j) was "
                "selected for the fixture project.",
                "2026-09-01",
            ),
        ),
        facts=(
            ExtractedFact(
                subject="Fixture Project",
                predicate=Predicate.SELECTED_OPTION,
                object="Architecture A",
                statement="The fixture project selected Architecture A.",
                valid_from_if_stated="2026-09-01",
            ),
        ),
    ),
    "architecture-decision-b.md": _Scripted(
        entities=(
            ("Fixture Project", EntityType.PROJECT),
            ("Architecture B", EntityType.CONCEPT),
            ("PostgreSQL", EntityType.TECHNOLOGY),
        ),
        artifacts=(
            _decision(
                "Architecture A abandoned",
                "Architecture A was abandoned after the graph could not be rebuilt.",
                "2026-09-10",
            ),
            _decision(
                "Architecture B selected",
                "Architecture B (PostgreSQL as the system of record, Neo4j as a rebuildable "
                "projection) was selected.",
                "2026-09-11",
                supersedes="Architecture A selected",
            ),
        ),
        facts=(
            ExtractedFact(
                subject="Fixture Project",
                predicate=Predicate.SELECTED_OPTION,
                object="Architecture B",
                statement="The fixture project selected Architecture B.",
                valid_from_if_stated="2026-09-11",
            ),
        ),
    ),
}


@dataclass
class StubKnowledgeEngine:
    """A :class:`~aimemory.domain.ports.KnowledgeEngine` whose "extraction" is a lookup table.

    ``kind`` is ``NATIVE`` because ADR-0009 settled on the native engine and the persisted rows have
    to carry a legal ``engine`` value; nothing about the stub pretends to be that engine's logic.
    """

    model_id: str = STUB_MODEL_ID
    seen: list[str] = field(default_factory=list)
    fail_on: tuple[str, ...] = ()

    @property
    def kind(self) -> EngineKind:
        return EngineKind.NATIVE

    def model_identity(self) -> ExtractionModel:
        return ExtractionModel(id=self.model_id, provider="stub", name=self.model_id)

    def process_episode(self, episode: Episode, context: Any) -> ExtractionResult:
        title = str(episode.title or "")
        self.seen.append(title)
        if title in self.fail_on:
            return ExtractionResult(
                episode_id=episode.id,
                engine=EngineKind.NATIVE,
                extraction_model_id=self.model_id,
                valid=False,
                errors=["JSONDecodeError: Expecting property name enclosed in double quotes"],
            )
        scripted = SCRIPT.get(title, _Scripted())
        return ExtractionResult(
            episode_id=episode.id,
            engine=EngineKind.NATIVE,
            extraction_model_id=self.model_id,
            summary=f"stub summary of {title}",
            entities=[
                ExtractedEntity(name=name, type=type_) for name, type_ in scripted.entities
            ],
            artifacts=list(scripted.artifacts),
            facts=list(scripted.facts),
            valid=True,
        )

    def invalidate(self, fact_id: UUID, at: datetime, by_episode: UUID | None = None) -> None:
        raise NotImplementedError  # not exercised by these scenarios


# ==================================================================================================
# The writer (a stand-in for A08's persistence, built on A04's primitives)
# ==================================================================================================


def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _provenance(session: Session, episode: Episode, result: ExtractionResult) -> Provenance:
    """The full plan section J stamp, assembled from the episode's own source version.

    Every field here comes from rows ingestion wrote; if any of them were missing,
    ``Provenance.is_complete`` would be false and scenario 11 would fail - which is the point of
    that scenario.
    """
    row = session.execute(
        sa.text(
            """
            SELECT s.id, s.uri, v.content_hash, v.id, s.project_id, r.device_id
              FROM source_versions v
              JOIN sources s ON s.id = v.source_id
              JOIN source_roots r ON r.root_id = s.root_id
             WHERE v.id = :vid
            """
        ),
        {"vid": episode.version_id},
    ).first()
    if row is None:  # pragma: no cover - only for manual/MCP episodes, which these tests do not use
        raise AssertionError("episode has no source version: ingestion did not write provenance")
    return Provenance(
        source_id=row[0],
        source_uri=row[1],
        source_hash=row[2],
        source_version=row[3],
        project_id=row[4] or episode.project_id,
        device_id=row[5],
        observed_at=episode.observed_at,
        extraction_model_id=result.extraction_model_id,
        ingestion_run_id=episode.ingestion_run_id,
        episode_id=episode.id,
    )


def _resolve_entity(
    session: Session, name: str, type_: EntityType, project_id: str | None, now: datetime
) -> Entity:
    """Deterministic-first resolution + the ADR-0014 rule 3 write-time type guard.

    :func:`aimemory.sources.seeds.resolve_entity_type` raises when the proposed type contradicts a
    Tier 0 seed. That call is the whole enforcement point; A08's engine makes the same one.
    """
    checked = resolve_entity_type(session, name, type_)
    repo = EntityRepo(session)
    existing = repo.find_by_normalized_name(checked.value, normalize_name(name), project_id)
    if existing is not None:
        return existing
    return repo.upsert(
        Entity(
            id=new_id(),
            type=checked,
            canonical_name=name,
            normalized_name=normalize_name(name),
            project_id=project_id,
            engine=EngineKind.NATIVE,
            first_seen_at=now,
            last_seen_at=now,
        )
    )


def _link_supersession(session: Session, artifact: KnowledgeArtifact) -> None:
    """Resolve a stated supersession in whichever direction the two artifacts arrived.

    ADR-0005 rule 6: the superseded row transitions to ``superseded`` with ``valid_to`` set and keeps
    every column it had. Nothing is deleted, and the link is written both ways
    (``supersedes_id`` / ``superseded_by_id``) so ``explain()`` can walk it from either end.
    """
    claimed = (artifact.structured or {}).get("supersedes_title")
    if claimed:
        older = session.execute(
            sa.text(
                "SELECT id, valid_from FROM knowledge_artifacts WHERE title = :t AND id <> :self "
                "AND current_status = 'current' ORDER BY valid_from LIMIT 1"
            ),
            {"t": claimed, "self": artifact.id},
        ).first()
        if older is not None:
            ArtifactRepo(session).supersede(older[0], artifact.id, artifact.valid_from)
            session.execute(
                sa.text("UPDATE knowledge_artifacts SET supersedes_id = :old WHERE id = :new"),
                {"old": older[0], "new": artifact.id},
            )
    # The reverse: an artifact already stored may have claimed to supersede the one just written.
    newer = session.execute(
        sa.text(
            "SELECT id, valid_from FROM knowledge_artifacts "
            "WHERE structured->>'supersedes_title' = :t AND id <> :self "
            "ORDER BY valid_from DESC LIMIT 1"
        ),
        {"t": artifact.title, "self": artifact.id},
    ).first()
    if newer is not None:
        ArtifactRepo(session).supersede(artifact.id, newer[0], newer[1])
        session.execute(
            sa.text("UPDATE knowledge_artifacts SET supersedes_id = :old WHERE id = :new"),
            {"old": artifact.id, "new": newer[0]},
        )


def persist_result(session: Session, result: ExtractionResult, episode: Episode) -> None:
    """Write one :class:`ExtractionResult` the way ADR-0005 requires. Minimal, but not simplified.

    * entities: resolved deterministically, type-guarded against the Tier 0 seeds;
    * artifacts: inserted ``current``; an artifact that states it supersedes an earlier title closes
      that one (``superseded``, ``valid_to`` set) instead of deleting or overwriting it (rule 6);
    * facts: a functional predicate that already has an open fact with a *different* object is
      closed at the new fact's ``valid_from`` and the new one records ``supersedes_fact_id``
      (rules 1 and 2) - which is also why the ``uq_facts_functional_current`` index never fires.
    """
    now = episode.observed_at
    provenance = _provenance(session, episode, result)
    project_id = provenance.project_id
    entities: dict[str, Entity] = {}
    for extracted in result.entities:
        entity = _resolve_entity(session, extracted.name, extracted.type, project_id, now)
        entities[normalize_name(extracted.name)] = entity

    artifact_repo = ArtifactRepo(session)
    for extracted in result.artifacts:
        valid_from = _iso(extracted.date_if_stated) or now
        artifact = artifact_repo.insert(
            KnowledgeArtifact(
                id=new_id(),
                type=extracted.artifact_type,
                title=extracted.title,
                body=extracted.statement,
                # The stated supersession is kept on the row, not only acted on, because episodes
                # arrive in queue order and the superseding document can be processed *before* the
                # one it supersedes. Keeping the claim lets the link be made from either side.
                structured=(
                    {"supersedes_title": extracted.supersedes_if_stated}
                    if extracted.supersedes_if_stated
                    else {}
                ),
                project_id=project_id,
                current_status=ArtifactStatus.CURRENT,
                valid_from=valid_from,
                evidence_quote=extracted.evidence_quote,
                engine=EngineKind.NATIVE,
                source_status=SourceStatus.ACTIVE,
                provenance=provenance.model_copy(update={"valid_from": valid_from}),
            )
        )
        _link_supersession(session, artifact)

    fact_repo = FactRepo(session)
    for extracted in result.facts:
        subject = entities.get(normalize_name(extracted.subject))
        obj = entities.get(normalize_name(extracted.object))
        if subject is None:
            continue  # unresolvable triples are dropped, never guessed
        valid_from = _iso(extracted.valid_from_if_stated) or now
        open_fact = fact_repo.find_open_fact(subject.id, extracted.predicate.value)
        supersedes: UUID | None = None
        valid_to: datetime | None = None
        status = FactStatus.CURRENT
        if open_fact is not None:
            same_object = (
                open_fact.object_entity_id == (obj.id if obj else None)
                and open_fact.object_value == (None if obj else extracted.object)
            )
            if same_object:
                fact_repo.touch(
                    open_fact.id,
                    observed_at=now,
                    status=FactStatus.CURRENT.value,
                    confidence=max(open_fact.confidence, extracted.confidence or 1.0),
                )
                continue
            if valid_from < open_fact.valid_from:
                # Out-of-order arrival: the queue handed us the *older* statement second. The open
                # fact keeps the present; the arriving one is written already-closed at the instant
                # the open one began, so history is recorded rather than the newer fact clobbered.
                valid_to = open_fact.valid_from
                status = FactStatus.HISTORICAL
            else:
                # Conflict on a functional predicate: close the open one at the new one's start
                # instant and chain them. Nothing is overwritten and nothing is deleted.
                fact_repo.close_fact(open_fact.id, valid_from, episode.id)
                supersedes = open_fact.id
        fact_repo.insert(
            Fact(
                id=new_id(),
                subject_entity_id=subject.id,
                predicate=extracted.predicate,
                object_entity_id=obj.id if obj else None,
                object_value=None if obj else extracted.object,
                statement=extracted.statement,
                valid_from=valid_from,
                valid_to=valid_to,
                observed_at=now,
                status=status,
                source_status=SourceStatus.ACTIVE,
                supersedes_fact_id=supersedes,
                confidence=extracted.confidence or 1.0,
                engine=EngineKind.NATIVE,
                project_id=project_id,
                provenance=provenance.model_copy(
                    update={"valid_from": valid_from, "valid_to": valid_to}
                ),
            )
        )
