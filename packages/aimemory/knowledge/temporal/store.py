"""Storage primitives the ADR-0005 rules compose (A08, P8-T01).

``docs/architecture/temporal.md`` writes the supersession algorithm against an abstract ``repo``. This
module is that abstraction: a :class:`FactStore` protocol with exactly the operations the pseudocode
names, plus :class:`SqlFactStore`, the PostgreSQL implementation that delegates to A04's
:class:`~aimemory.persistence.repositories.FactRepo` wherever A04 already offers the primitive and
adds the two lookups the algorithm needs that the repo does not expose
(:meth:`find_open_fact_with_object` for the non-functional branch and :meth:`facts_from_version` for
rule 3).

Why a protocol rather than "just use the repo": the rules are pure decision logic and must be
testable without a database, but the *ordering* against the live partial unique index
``uq_facts_functional_current`` must be testable **with** one. Both test styles bind to this
interface, so the rules module has exactly one code path.

Transaction contract, and it is not negotiable: ``close()`` and the following ``insert()`` of a
superseding functional fact run in **one** transaction. The partial unique index on
``(subject_entity_id, predicate) WHERE valid_to IS NULL AND predicate IN <functional>`` rejects the
insert otherwise. A :class:`SqlFactStore` therefore never commits on its own - the caller owns the
session and the transaction boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...common.time import ensure_utc
from ...domain.enums import FactStatus, SourceStatus
from ...domain.models import Fact
from ...domain.provenance import Provenance
from ...persistence.repositories import FactRepo

__all__ = ["FactStore", "GraphSink", "SqlFactStore", "fact_fingerprint", "fact_from_row"]


def fact_from_row(row: object) -> Fact:
    """A ``facts`` row (mapping or SQLAlchemy Row) to a :class:`Fact` with its ``[PROV]`` stamp.

    Deliberately written here instead of reusing A04's private ``_fact_from_row``: this module must
    not depend on a private symbol in a file another agent owns.
    """
    m = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)  # type: ignore[union-attr]
    return Fact(
        id=m["id"],
        subject_entity_id=m["subject_entity_id"],
        predicate=m["predicate"],
        object_entity_id=m.get("object_entity_id"),
        object_value=m.get("object_value"),
        statement=m["statement"],
        valid_from=m["valid_from"],
        valid_to=m.get("valid_to"),
        observed_at=m["observed_at"],
        status=m["status"],
        source_status=m["source_status"],
        invalidated_at=m.get("invalidated_at"),
        invalidated_by_episode_id=m.get("invalidated_by_episode_id"),
        supersedes_fact_id=m.get("supersedes_fact_id"),
        confidence=m["confidence"],
        engine=m["engine"],
        project_id=m.get("project_id"),
        provenance=Provenance(
            source_id=m.get("source_id"),
            source_uri=m.get("source_uri"),
            source_hash=m.get("source_hash"),
            source_version=m.get("source_version"),
            project_id=m.get("project_id"),
            device_id=m["device_id"],
            observed_at=m["observed_at"],
            valid_from=m["valid_from"],
            valid_to=m.get("valid_to"),
            confidence=m["confidence"],
            extraction_model_id=m.get("extraction_model_id"),
            embedding_model_id=m.get("embedding_model_id"),
            ingestion_run_id=m.get("ingestion_run_id"),
            episode_id=m.get("episode_id"),
            heading_path=list(m.get("heading_path") or []),
        ),
    )


def fact_fingerprint(fact: Fact) -> tuple[str, str, str]:
    """``(subject, predicate, object_key)`` - the identity rule 3 compares versions on."""
    return (str(fact.subject_entity_id), str(fact.predicate), fact.object_key)


@runtime_checkable
class FactStore(Protocol):
    """The operations ``apply_fact`` / ``reconcile_version`` / ``mark_source_deleted`` need."""

    def get(self, fact_id: UUID) -> Fact | None: ...

    def find_open_fact(self, subject_entity_id: UUID, predicate: str) -> Fact | None: ...

    def find_open_fact_with_object(
        self, subject_entity_id: UUID, predicate: str, object_key: str
    ) -> Fact | None: ...

    def insert(self, fact: Fact) -> Fact: ...

    def close_fact(self, fact_id: UUID, at: datetime, by_episode: UUID | None) -> Fact: ...

    def touch(
        self, fact_id: UUID, *, observed_at: datetime, status: str, confidence: float
    ) -> Fact: ...

    def facts_from_version(self, version_id: UUID) -> list[Fact]: ...

    def set_status(self, fact_id: UUID, status: FactStatus) -> Fact | None: ...

    def mark_source_status(self, source_id: UUID, status: SourceStatus) -> int: ...


@runtime_checkable
class GraphSink(Protocol):
    """The one thing the temporal rules ask of Neo4j: close an edge, never delete it."""

    def invalidate_relationship(self, fact_id: UUID | str, at: datetime) -> bool: ...


class SqlFactStore:
    """PostgreSQL :class:`FactStore`. Never commits; the caller owns the transaction."""

    def __init__(self, session: Session) -> None:
        self._s = session
        self._repo = FactRepo(session)

    # ---- reads ------------------------------------------------------------------------------

    def get(self, fact_id: UUID) -> Fact | None:
        row = self._s.execute(
            text("SELECT * FROM facts WHERE id = :id"), {"id": str(fact_id)}
        ).first()
        return fact_from_row(row) if row is not None else None

    def find_open_fact(self, subject_entity_id: UUID, predicate: str) -> Fact | None:
        return self._repo.find_open_fact(subject_entity_id, str(predicate))

    def find_open_fact_with_object(
        self, subject_entity_id: UUID, predicate: str, object_key: str
    ) -> Fact | None:
        """The non-functional branch: the *same triple* already open, or nothing.

        ``object_key`` is compared against ``object_entity_id`` first and ``object_value`` second,
        mirroring :attr:`aimemory.domain.models.Fact.object_key` exactly.
        """
        row = self._s.execute(
            text(
                """
                SELECT * FROM facts
                 WHERE subject_entity_id = :subject AND predicate = :predicate
                   AND valid_to IS NULL
                   AND coalesce(object_entity_id::text, object_value) = :object_key
                 ORDER BY valid_from DESC
                 LIMIT 1
                """
            ),
            {
                "subject": str(subject_entity_id),
                "predicate": str(predicate),
                "object_key": object_key,
            },
        ).first()
        return fact_from_row(row) if row is not None else None

    def facts_from_version(self, version_id: UUID) -> list[Fact]:
        """Rule 3 input: the still-live facts derived from one source version."""
        rows = self._s.execute(
            text(
                """
                SELECT * FROM facts
                 WHERE source_version = :vid
                   AND status IN ('current', 'unconfirmed')
                   AND valid_to IS NULL
                 ORDER BY valid_from
                """
            ),
            {"vid": str(version_id)},
        ).all()
        return [fact_from_row(row) for row in rows]

    def open_facts_for_source(self, source_id: UUID) -> list[Fact]:
        rows = self._s.execute(
            text(
                "SELECT * FROM facts WHERE source_id = :sid AND valid_to IS NULL ORDER BY valid_from"
            ),
            {"sid": str(source_id)},
        ).all()
        return [fact_from_row(row) for row in rows]

    # ---- writes -----------------------------------------------------------------------------

    def insert(self, fact: Fact) -> Fact:
        return self._repo.insert(fact)

    def close_fact(self, fact_id: UUID, at: datetime, by_episode: UUID | None) -> Fact:
        return self._repo.close_fact(fact_id, ensure_utc(at), by_episode)

    def touch(self, fact_id: UUID, *, observed_at: datetime, status: str, confidence: float) -> Fact:
        return self._repo.touch(
            fact_id,
            observed_at=ensure_utc(observed_at),
            status=str(status),
            confidence=confidence,
        )

    def set_status(self, fact_id: UUID, status: FactStatus) -> Fact | None:
        row = self._s.execute(
            text("UPDATE facts SET status = :status WHERE id = :id RETURNING *"),
            {"id": str(fact_id), "status": str(status)},
        ).first()
        return fact_from_row(row) if row is not None else None

    def mark_source_status(self, source_id: UUID, status: SourceStatus) -> int:
        """Rule 4: flag derived rows; ``valid_from`` / ``valid_to`` / ``status`` are not touched."""
        facts = self._s.execute(
            text("UPDATE facts SET source_status = :status WHERE source_id = :sid"),
            {"sid": str(source_id), "status": str(status)},
        ).rowcount
        artifacts = self._s.execute(
            text("UPDATE knowledge_artifacts SET source_status = :status WHERE source_id = :sid"),
            {"sid": str(source_id), "status": str(status)},
        ).rowcount
        return int(facts or 0) + int(artifacts or 0)

    def find_facts_for_subjects(
        self, subject_ids: Sequence[UUID], *, as_of: datetime | None = None
    ) -> list[Fact]:
        if not subject_ids:
            return []
        rows = self._s.execute(
            text(
                """
                SELECT * FROM facts
                 WHERE subject_entity_id = ANY(:ids)
                   AND (:as_of IS NULL
                        OR (valid_from <= :as_of AND (valid_to IS NULL OR valid_to > :as_of)))
                 ORDER BY valid_from
                """
            ),
            {
                "ids": [str(i) for i in subject_ids],
                "as_of": ensure_utc(as_of) if as_of else None,
            },
        ).all()
        return [fact_from_row(row) for row in rows]
