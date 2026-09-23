"""ADR-0005 rule 6 for knowledge artifacts: **supersession is a state, not a deletion** (A08).

A decision that has been replaced is still in the memory. Its ``current_status`` becomes
``superseded``, its ``valid_to`` is set to the moment the replacement became valid, and
``supersedes_id`` / ``superseded_by_id`` form a chain that ``memory.get_artifact`` walks. Asking the
system "what did we decide in September" at an ``as_of`` inside the old window still returns the old
decision, which is the entire point of keeping it.

This is the artifact twin of :mod:`aimemory.knowledge.temporal.rules`, and it follows the same
ordering discipline: resolve the target, close it, insert the replacement with the link, all in one
transaction.

Resolution of an explicitly stated supersession (``ExtractedArtifact.supersedes_if_stated`` carries a
*title*, because the model has no ids) is deliberately conservative: exact case-insensitive title
match inside the same project. A near-miss is reported as a warning and the new artifact is written
as an ordinary current one - guessing which decision was meant would corrupt the timeline in a way
that is very hard to see later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...common.logging import get_logger
from ...common.time import ensure_utc
from ...domain.enums import ArtifactStatus
from ...domain.models import ArtifactEntity, KnowledgeArtifact
from ...persistence.repositories import ArtifactRepo

__all__ = [
    "ArtifactOutcome",
    "ArtifactStore",
    "SqlArtifactStore",
    "apply_artifact",
    "reconcile_artifacts",
]

logger = get_logger(__name__)


@runtime_checkable
class ArtifactStore(Protocol):
    """What rule 6 needs. A04's ``ArtifactRepo`` supplies most of it; the rest is below."""

    def insert(self, artifact: KnowledgeArtifact) -> KnowledgeArtifact: ...

    def get(self, artifact_id: UUID) -> KnowledgeArtifact | None: ...

    def supersede(self, old_id: UUID, new_id: UUID, at: datetime) -> None: ...

    def link_entity(self, artifact_entity: ArtifactEntity) -> None: ...

    def find_by_title(
        self, title: str, *, project_id: str | None = None, exclude_id: UUID | None = None
    ) -> KnowledgeArtifact | None: ...

    def set_status(self, artifact_id: UUID, status: ArtifactStatus) -> None: ...

    def artifacts_from_version(self, version_id: UUID) -> list[KnowledgeArtifact]: ...


@dataclass(slots=True)
class ArtifactOutcome:
    """The stored artifact plus what it replaced."""

    artifact: KnowledgeArtifact
    action: str = "added"  # added | superseded
    superseded_id: UUID | None = None
    warnings: list[str] = field(default_factory=list)


def apply_artifact(
    artifact: KnowledgeArtifact,
    *,
    store: ArtifactStore,
    supersedes_id: UUID | None = None,
    supersedes_title: str | None = None,
    entity_links: Sequence[tuple[UUID, str]] = (),
) -> ArtifactOutcome:
    """Insert ``artifact``, honouring an explicit supersession (ADR-0005 rules 2 and 6).

    ``supersedes_id`` (from ``record_decision(supersedes=...)``) beats ``supersedes_title`` (from the
    model's ``supersedes_if_stated``), because an id cannot be ambiguous and a title can.
    """
    warnings: list[str] = []
    target: KnowledgeArtifact | None = None

    if supersedes_id is not None:
        target = store.get(supersedes_id)
        if target is None:
            warnings.append(f"stated supersession target id {supersedes_id} not found")
    if target is None and supersedes_title:
        target = store.find_by_title(
            supersedes_title, project_id=artifact.project_id, exclude_id=artifact.id
        )
        if target is None:
            warnings.append(
                f"stated supersession target title {supersedes_title!r} not found; "
                "artifact written as current without a chain link"
            )

    if target is not None and target.id == artifact.id:
        warnings.append("artifact cannot supersede itself; link ignored")
        target = None

    stored = store.insert(
        artifact.model_copy(
            update={
                "supersedes_id": target.id if target else artifact.supersedes_id,
                "current_status": artifact.current_status,
            }
        )
    )
    for entity_id, role in entity_links:
        store.link_entity(ArtifactEntity(artifact_id=stored.id, entity_id=entity_id, role=role))

    if target is None:
        return ArtifactOutcome(stored, action="added", warnings=warnings)

    at = ensure_utc(stored.valid_from)
    if at < ensure_utc(target.valid_from):
        warnings.append(
            "supersession target is newer than its replacement; the target was left current"
        )
        return ArtifactOutcome(stored, action="added", warnings=warnings)

    store.supersede(target.id, stored.id, at)
    logger.info(
        "temporal.artifact_superseded",
        artifact_id=str(stored.id),
        superseded_id=str(target.id),
        at=at.isoformat(),
    )
    return ArtifactOutcome(stored, action="superseded", superseded_id=target.id, warnings=warnings)


def reconcile_artifacts(
    *,
    old_version_id: UUID,
    reextracted_titles: Sequence[str],
    store: ArtifactStore,
) -> list[UUID]:
    """Rule 3 for artifacts: not re-stated in the new version -> ``unconfirmed``, never deleted."""
    wanted = {title.strip().lower() for title in reextracted_titles}
    changed: list[UUID] = []
    for artifact in store.artifacts_from_version(old_version_id):
        if artifact.title.strip().lower() in wanted:
            continue
        store.set_status(artifact.id, ArtifactStatus.UNCONFIRMED)
        changed.append(artifact.id)
    return changed


class SqlArtifactStore:
    """PostgreSQL :class:`ArtifactStore`. Never commits; the caller owns the transaction."""

    def __init__(self, session: Session) -> None:
        self._s = session
        self._repo = ArtifactRepo(session)

    def insert(self, artifact: KnowledgeArtifact) -> KnowledgeArtifact:
        return self._repo.insert(artifact)

    def get(self, artifact_id: UUID) -> KnowledgeArtifact | None:
        return self._repo.get(artifact_id)

    def supersede(self, old_id: UUID, new_id: UUID, at: datetime) -> None:
        self._repo.supersede(old_id, new_id, ensure_utc(at))

    def link_entity(self, artifact_entity: ArtifactEntity) -> None:
        self._repo.link_entity(artifact_entity)

    def set_status(self, artifact_id: UUID, status: ArtifactStatus) -> None:
        self._s.execute(
            text("UPDATE knowledge_artifacts SET current_status = :status WHERE id = :id"),
            {"id": str(artifact_id), "status": str(status)},
        )

    def find_by_title(
        self, title: str, *, project_id: str | None = None, exclude_id: UUID | None = None
    ) -> KnowledgeArtifact | None:
        row = self._s.execute(
            text(
                """
                SELECT id FROM knowledge_artifacts
                 WHERE lower(title) = lower(:title)
                   AND (CAST(:project_id AS text) IS NULL OR project_id = :project_id)
                   AND (CAST(:exclude AS uuid) IS NULL OR id <> :exclude)
                 ORDER BY valid_from DESC
                 LIMIT 1
                """
            ),
            {
                "title": title,
                "project_id": project_id,
                "exclude": str(exclude_id) if exclude_id else None,
            },
        ).first()
        return self._repo.get(row[0]) if row is not None else None

    def artifacts_from_version(self, version_id: UUID) -> list[KnowledgeArtifact]:
        rows = self._s.execute(
            text(
                """
                SELECT id FROM knowledge_artifacts
                 WHERE source_version = :vid
                   AND current_status IN ('current', 'unconfirmed')
                   AND valid_to IS NULL
                """
            ),
            {"vid": str(version_id)},
        ).all()
        found: list[KnowledgeArtifact] = []
        for row in rows:
            artifact = self._repo.get(row[0])
            if artifact is not None:
                found.append(artifact)
        return found

    def chain(self, artifact_id: UUID) -> list[dict[str, Any]]:
        """The supersession chain for ``memory.get_artifact``: newest first, oldest last."""
        rows = self._s.execute(
            text(
                """
                WITH RECURSIVE back AS (
                    SELECT id, title, current_status, valid_from, valid_to, supersedes_id, 0 AS depth
                      FROM knowledge_artifacts WHERE id = :id
                    UNION ALL
                    SELECT a.id, a.title, a.current_status, a.valid_from, a.valid_to,
                           a.supersedes_id, back.depth + 1
                      FROM knowledge_artifacts a JOIN back ON a.id = back.supersedes_id
                )
                SELECT * FROM back ORDER BY depth
                """
            ),
            {"id": str(artifact_id)},
        ).mappings().all()
        return [dict(row) for row in rows]
