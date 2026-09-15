"""Deterministic entity seeds - ADR-0014 rule 3 (A07a, Tier 0).

Where a correct answer is already known, no model gets a vote. P4-T02 MEASURED both extraction
providers mistyping the *same* things: the vault's PARA folders come back as ``Repository`` from
``qwen3:4b`` and as ``InfrastructureComponent`` from Claude Haiku 4.5, when they are plainly
``Document``/``Concept``. That is not a prompt-tuning problem - it is a fact about the corpus that
the ingestion side already knows for free, so it is written deterministically at Tier 0 and then
**defended at write time**.

Two things live here:

``seed_deterministic_entities``
    Writes the PARA folder names and the registry projects into ``entities`` with
    ``engine='deterministic'`` (the ``extraction_models`` row ``deterministic:registry-v1`` is the
    provenance stamp for anything derived from them). Idempotent: the ids are UUIDv5 of the
    ``(type, normalized name)`` pair and the write is A04's ``EntityRepo.upsert``.

``resolve_entity_type`` / ``assert_entity_type_allowed``
    The write-time guard ADR-0014 requires: *"Entity type is rejected at write time when it
    contradicts a deterministic seed. The LLM may add entities, never retype a seeded one."* A08's
    engine calls :func:`resolve_entity_type` before writing an entity; a proposed type that
    contradicts a seed raises :class:`SeedTypeConflict`, and a type inside the seed's allowed set is
    snapped to the seed's canonical type instead of forking a second row (the uniqueness key is
    ``(type, normalized_name, project)``, so a retyping *would* silently create a twin).

Deliberately not here: any LLM call, any Neo4j write, and any fact. Tier 0 produces nodes that are
true by construction; edges between them are the structural projection (A08, ``knowledge/structural``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.errors import AiMemoryError
from ..common.ids import deterministic_id, normalize_name
from ..common.logging import get_logger
from ..common.time import utc_now
from ..domain.enums import EngineKind, EntityType
from ..domain.models import Entity
from ..domain.provenance import DETERMINISTIC_MODEL_ID
from ..persistence.repositories import EntityRepo

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .roots import RootContext

__all__ = [
    "DETERMINISTIC_MODEL_ID",
    "PARA_FOLDERS",
    "SeedType",
    "SeedTypeConflict",
    "assert_entity_type_allowed",
    "para_seeds",
    "resolve_entity_type",
    "seed_deterministic_entities",
]

logger = get_logger(__name__)


class SeedTypeConflict(AiMemoryError):
    """An entity write proposed a type that contradicts a deterministic Tier 0 seed.

    ``AiMemoryError.__str__`` shows ``public_message`` only, so the two type names and the remedy go
    there: this message is what an operator sees when an extraction run is stopped by the guard.
    """

    code = "seed_type_conflict"
    http_status = 409


@dataclass(frozen=True)
class SeedType:
    """The authoritative typing of one seeded name.

    ``canonical`` is what gets written. ``allowed`` is the set a caller may propose without being
    rejected - ADR-0014 names *both* ``Document`` and ``Concept`` as correct for a PARA folder, so a
    model that says "Document" is not wrong, it is merely less specific; it is snapped to the
    canonical type rather than creating a second row under the ``(type, name, project)`` key.
    """

    canonical: EntityType
    allowed: frozenset[EntityType]
    reason: str

    def check(self, proposed: EntityType) -> EntityType:
        if proposed in self.allowed:
            return self.canonical
        raise SeedTypeConflict(
            f"entity type {proposed.value!r} contradicts the deterministic seed "
            f"{self.canonical.value!r} ({self.reason}); allowed types: "
            f"{sorted(t.value for t in self.allowed)}. ADR-0014 rule 3: the extraction model may add "
            "entities, never retype a seeded one.",
            detail=f"seed canonical={self.canonical.value} proposed={proposed.value}",
            context={"seeded_type": self.canonical.value, "proposed_type": proposed.value},
        )


#: The vault's PARA top-level folders (plan section K / ADR-0006). Typed ``Concept`` - they are
#: organising categories, not files - with ``Document`` accepted as the second reading ADR-0014
#: allows. Both providers instead produced ``Repository`` / ``InfrastructureComponent`` (MEASURED,
#: P4-T02), which is exactly what the guard below now rejects.
PARA_FOLDERS: tuple[str, ...] = (
    "00 Inbox",
    "01 Projects",
    "02 Areas",
    "03 Resources",
    "04 Archives",
    "05 Templates",
    "06 Outputs",
    "07 Workflows",
)

_PARA_SEED = SeedType(
    canonical=EntityType.CONCEPT,
    allowed=frozenset({EntityType.CONCEPT, EntityType.DOCUMENT}),
    reason="PARA folder of the vault, seeded deterministically at Tier 0",
)

_PROJECT_SEED = SeedType(
    canonical=EntityType.PROJECT,
    allowed=frozenset({EntityType.PROJECT, EntityType.SUB_PROJECT}),
    reason="project from the AIOS registry (AIOS/me.md, AIOS/Maps/project-graph.md)",
)


def para_seeds() -> dict[str, SeedType]:
    """``normalized folder name -> SeedType`` for the eight PARA folders."""
    return {normalize_name(name): _PARA_SEED for name in PARA_FOLDERS}


# --------------------------------------------------------------------------------------------------
# Write-time guard (called by A07a's Tier 0 and by A08's engine before every entity write)
# --------------------------------------------------------------------------------------------------


def _seeded_type_from_db(session: Session, normalized: str) -> EntityType | None:
    """The type of an existing ``engine='deterministic'`` row with this normalized name, if any."""
    row = session.execute(
        text(
            "SELECT type FROM entities WHERE normalized_name = :n AND engine = 'deterministic' "
            "AND merged_into_id IS NULL ORDER BY first_seen_at LIMIT 1"
        ),
        {"n": normalized},
    ).first()
    return EntityType(row[0]) if row is not None else None


def resolve_entity_type(
    session: Session | None, name: str, proposed: EntityType | str
) -> EntityType:
    """The type an entity named ``name`` must be written with.

    Returns ``proposed`` unchanged for names nobody seeded - the common case, and the reason the LLM
    can still add entities freely. Raises :class:`SeedTypeConflict` when a seed exists and
    ``proposed`` is outside its allowed set.

    ``session`` may be ``None`` to check against the static table only (no database round-trip),
    which is what the unit tests and the CLI dry run use.
    """
    proposed_type = EntityType(proposed)
    normalized = normalize_name(name)
    seed = para_seeds().get(normalized)
    if seed is not None:
        return seed.check(proposed_type)
    if session is None:
        return proposed_type
    seeded = _seeded_type_from_db(session, normalized)
    if seeded is None or seeded is proposed_type:
        return proposed_type
    dynamic = SeedType(
        canonical=seeded,
        allowed=(
            _PROJECT_SEED.allowed
            if seeded in (EntityType.PROJECT, EntityType.SUB_PROJECT)
            else frozenset({seeded})
        ),
        reason="written deterministically at Tier 0 (engine='deterministic')",
    )
    return dynamic.check(proposed_type)


def assert_entity_type_allowed(
    session: Session | None, name: str, proposed: EntityType | str
) -> None:
    """:func:`resolve_entity_type` as a bare assertion, for callers that only want the check."""
    resolve_entity_type(session, name, proposed)


# --------------------------------------------------------------------------------------------------
# Tier 0 seeding
# --------------------------------------------------------------------------------------------------


def _entity(
    name: str, entity_type: EntityType, *, project_id: str | None, now: datetime, summary: str
) -> Entity:
    normalized = normalize_name(name)
    return Entity(
        # UUIDv5 on (type, normalized name, project): re-seeding rewrites the same row, and the id is
        # reproducible from the name alone, which is what makes the seed auditable.
        id=deterministic_id("entity", entity_type.value, normalized, project_id),
        type=entity_type,
        canonical_name=name,
        normalized_name=normalized,
        aliases=[],
        project_id=project_id,
        summary=summary,
        engine=EngineKind.DETERMINISTIC,
        confidence=1.0,
        first_seen_at=now,
        last_seen_at=now,
    )


def seed_deterministic_entities(
    session: Session,
    ctx: RootContext | None = None,
    *,
    project_ids: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Write the ADR-0014 rule 3 seeds. Idempotent; returns counters for ``ingestion_runs``.

    * the eight PARA folder names -> ``Concept`` (allowed: ``Concept``/``Document``)
    * every registry project -> ``Project``, stamped ``deterministic:registry-v1``

    Only folders that actually exist in this root are seeded, so a repository root does not acquire
    the vault's vocabulary. ``project_ids`` defaults to every row in ``projects`` (the registry has
    just written them when this runs).
    """
    now = now or utc_now()
    repo = EntityRepo(session)
    counters = {"entities_seeded_para": 0, "entities_seeded_projects": 0}

    for folder in PARA_FOLDERS:
        if ctx is not None and not (ctx.base_path / folder).is_dir():
            continue
        repo.upsert(
            _entity(
                folder,
                _PARA_SEED.canonical,
                project_id=None,
                now=now,
                summary=f"Vault PARA folder. {_PARA_SEED.reason}.",
            )
        )
        counters["entities_seeded_para"] += 1

    rows = session.execute(text("SELECT id, name FROM projects ORDER BY id")).all()
    wanted = set(project_ids) if project_ids is not None else None
    for project_id, project_name in rows:
        if wanted is not None and project_id not in wanted:
            continue
        repo.upsert(
            _entity(
                str(project_name or project_id),
                _PROJECT_SEED.canonical,
                project_id=str(project_id),
                now=now,
                summary=f"Registry project ({DETERMINISTIC_MODEL_ID}).",
            )
        )
        counters["entities_seeded_projects"] += 1

    logger.info(
        "ingestion.entities_seeded",
        para=counters["entities_seeded_para"],
        projects=counters["entities_seeded_projects"],
        model=DETERMINISTIC_MODEL_ID,
    )
    return counters
