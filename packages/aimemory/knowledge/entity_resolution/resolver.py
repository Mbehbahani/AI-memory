"""Deterministic-first entity resolution (A08, P8-T03).

Order of attempts, and the model is nowhere in the first four:

1. **normalize** - :func:`aimemory.common.ids.normalize_name` (case, accents, whitespace, trailing
   punctuation). Everything below compares normalized names.
2. **alias tables** - ``config/technology-aliases.yaml`` plus ``project_aliases`` (A07a's registry
   seed). A hit replaces both the name *and* the type.
3. **seed guard** - :func:`aimemory.sources.seeds.resolve_entity_type` (ADR-0014 rule 3): a proposed
   type that contradicts a deterministic Tier 0 seed is **rejected at write time**. The model may add
   entities; it may never retype a seeded one.
4. **exact** - ``(type, normalized_name, project)``, the table's own uniqueness key.
5. **trigram** - ``pg_trgm`` similarity within the same type, threshold ``>= 0.85``.
6. **LLM tie-break** - off by default (``llm_tiebreak=False``), and only ever consulted for a genuine
   near-miss band; it can never override steps 2-4.
7. **create**.

Why this order is not negotiable. MEASURED in P4-T02 (n=20 real my-vault episodes per provider),
both extraction providers make the *same systematic* type errors: the vault's PARA folders come back
as ``Repository`` (qwen3:4b) or ``InfrastructureComponent`` (Haiku 4.5) when they are
``Concept``/``Document``; ``Personal Harness`` comes back as ``Technology`` when it is a project;
qwen3 additionally typed the owner (``Mbehbahani``) as ``Organization`` and ``Mohammad`` as
``InfrastructureComponent``, and typed tools (``GitHub Copilot``, ``Claude Code``, ``OpenClaw``) as
``Project``. A person mistyped as an organization corrupts every ownership edge that touches them,
so :data:`PERSON_PROTECTED_FROM` makes that specific repair explicit rather than incidental.

Merges never delete: the loser keeps its row with ``merged_into_id`` set, so old provenance still
resolves (``entities.merged_into_id``; the plan calls it ``merged_into_entity_id`` in prose).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...common.errors import ContractError
from ...common.ids import deterministic_id, normalize_name
from ...common.logging import get_logger
from ...common.time import utc_now
from ...domain.enums import EngineKind, EntityType
from ...domain.models import Entity
from ...sources.seeds import SeedTypeConflict, resolve_entity_type
from .aliases import AliasIndex, load_alias_index

__all__ = [
    "PERSON_PROTECTED_FROM",
    "TRIGRAM_THRESHOLD",
    "EntityResolver",
    "EntityStore",
    "ResolvedEntity",
    "ResolutionMethod",
    "SqlEntityStore",
    "entity_id_for",
]

logger = get_logger(__name__)

#: ``pg_trgm`` similarity a same-type candidate must reach to be treated as the same entity.
TRIGRAM_THRESHOLD = 0.85

#: Types an existing ``Person`` is never re-typed into. MEASURED: qwen3:4b returned ``Organization``
#: for ``Mbehbahani`` and ``InfrastructureComponent`` for ``Mohammad``; either one silently moves
#: ``HAS_OWNER`` / ``CREATED_BY`` edges onto a node that is not a human.
PERSON_PROTECTED_FROM: frozenset[EntityType] = frozenset(
    {
        EntityType.ORGANIZATION,
        EntityType.INFRASTRUCTURE_COMPONENT,
        EntityType.TECHNOLOGY,
        EntityType.APPLICATION,
        EntityType.CONCEPT,
    }
)


class ResolutionMethod:
    """How a name was resolved. Plain strings so they can be counted in a run report."""

    ALIAS = "alias"
    SEED = "seed"
    EXACT = "exact"
    MERGED = "merged"
    PERSON_REPAIR = "person_repair"
    TRIGRAM = "trigram"
    LLM = "llm"
    CREATED = "created"


@dataclass(slots=True)
class ResolvedEntity:
    """The resolved row plus a full account of how it was reached."""

    entity: Entity
    method: str
    proposed_type: EntityType
    effective_type: EntityType
    created: bool = False
    type_overridden: bool = False
    similarity: float | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def id(self) -> UUID:
        return self.entity.id


def entity_id_for(entity_type: EntityType, normalized_name: str, project_id: str | None) -> UUID:
    """The deterministic id A07a's Tier 0 seed uses, so a seed and a resolution converge on one row.

    Must stay byte-identical to ``aimemory.sources.seeds._entity``: UUIDv5 over
    ``("entity", type, normalized name, project)``.
    """
    return deterministic_id("entity", entity_type.value, normalized_name, project_id)


@runtime_checkable
class EntityStore(Protocol):
    """The reads and writes the resolver needs. Implemented by :class:`SqlEntityStore`."""

    def get(self, entity_id: UUID) -> Entity | None: ...

    def find_exact(
        self, entity_type: EntityType, normalized_name: str, project_id: str | None
    ) -> Entity | None: ...

    def find_by_name(self, normalized_name: str) -> list[Entity]: ...

    def trigram_candidates(
        self, normalized_name: str, entity_type: EntityType, *, limit: int = 5
    ) -> list[tuple[Entity, float]]: ...

    def upsert(self, entity: Entity) -> Entity: ...

    def merge(self, loser_id: UUID, winner_id: UUID) -> None: ...


class EntityResolver:
    """Resolve extracted names to ``entities`` rows, deterministically wherever possible.

    One instance per episode (it caches within the episode so the same name is resolved once), and it
    never commits - the caller owns the transaction.
    """

    def __init__(
        self,
        store: EntityStore,
        *,
        session: Session | None = None,
        aliases: AliasIndex | None = None,
        project_id: str | None = None,
        engine: EngineKind = EngineKind.NATIVE,
        threshold: float = TRIGRAM_THRESHOLD,
        llm_tiebreak: bool = False,
        provider: Any = None,
        on_seed_conflict: str = "repair",
    ) -> None:
        if on_seed_conflict not in ("repair", "raise"):
            raise ContractError("on_seed_conflict must be 'repair' or 'raise'.")
        self._store = store
        self._session = session
        self._aliases = aliases if aliases is not None else load_alias_index()
        self._project_id = project_id
        self._engine = engine
        self._threshold = threshold
        self._llm_tiebreak = llm_tiebreak
        self._provider = provider
        self._on_seed_conflict = on_seed_conflict
        self._cache: dict[tuple[str, str], ResolvedEntity] = {}
        self.stats: dict[str, int] = {}

    # ---- public API ------------------------------------------------------------------------

    def resolve(
        self,
        name: str,
        proposed_type: EntityType | str,
        *,
        project_id: str | None = None,
        aliases: Sequence[str] = (),
        summary: str | None = None,
        observed_at: datetime | None = None,
        create: bool = True,
    ) -> ResolvedEntity | None:
        """Resolve one extracted name. ``None`` only when ``create=False`` and nothing matched."""
        raw = (name or "").strip()
        normalized = normalize_name(raw)
        if not normalized:
            return None
        proposed = EntityType(proposed_type)
        cache_key = (normalized, proposed.value)
        if cache_key in self._cache:
            return self._cache[cache_key]

        resolved = self._resolve_uncached(
            raw,
            normalized,
            proposed,
            project_id=project_id if project_id is not None else self._project_id,
            extra_aliases=list(aliases),
            summary=summary,
            observed_at=observed_at or utc_now(),
            create=create,
        )
        if resolved is not None:
            self._cache[cache_key] = resolved
            self.stats[resolved.method] = self.stats.get(resolved.method, 0) + 1
        return resolved

    def resolve_all(
        self, items: Sequence[tuple[str, EntityType | str]], **kwargs: Any
    ) -> dict[str, ResolvedEntity]:
        """Resolve a call-1 entity list; keyed by the *normalized* surface form for fact lookup."""
        out: dict[str, ResolvedEntity] = {}
        for name, entity_type in items:
            resolved = self.resolve(name, entity_type, **kwargs)
            if resolved is None:
                continue
            out[normalize_name(name)] = resolved
            for alias in resolved.entity.aliases:
                out.setdefault(normalize_name(alias), resolved)
            out.setdefault(normalize_name(resolved.entity.canonical_name), resolved)
        return out

    # ---- the ladder ------------------------------------------------------------------------

    def _resolve_uncached(
        self,
        raw: str,
        normalized: str,
        proposed: EntityType,
        *,
        project_id: str | None,
        extra_aliases: list[str],
        summary: str | None,
        observed_at: datetime,
        create: bool,
    ) -> ResolvedEntity | None:
        warnings: list[str] = []
        canonical_name = raw
        effective = proposed
        method = ResolutionMethod.CREATED
        type_overridden = False

        # --- 2. alias tables (deterministic; they carry the type) ---------------------------
        hit = self._aliases.lookup(raw)
        if hit is not None:
            if hit.entity_type is not proposed:
                type_overridden = True
                warnings.append(
                    f"alias table types {raw!r} as {hit.entity_type.value}; "
                    f"the model proposed {proposed.value}"
                )
            canonical_name = hit.canonical_name
            effective = hit.entity_type
            normalized = normalize_name(canonical_name)
            if hit.project_id:
                project_id = hit.project_id
            method = ResolutionMethod.ALIAS

        # --- 3. deterministic seed guard (ADR-0014 rule 3) -----------------------------------
        try:
            seeded = resolve_entity_type(self._session, canonical_name, effective)
        except SeedTypeConflict as conflict:
            if self._on_seed_conflict == "raise":
                raise
            seeded_type = EntityType(conflict.context.get("seeded_type", effective.value))
            warnings.append(
                f"rejected proposed type {effective.value!r} for {canonical_name!r}: it contradicts "
                f"the deterministic seed {seeded_type.value!r} (ADR-0014 rule 3)"
            )
            seeded = seeded_type
        if seeded is not effective:
            type_overridden = True
            effective = seeded
            method = ResolutionMethod.SEED

        # --- 4. exact match on the table's own uniqueness key --------------------------------
        found = self._store.find_exact(effective, normalized, project_id)
        if found is None and project_id is not None:
            found = self._store.find_exact(effective, normalized, None)
        if found is not None:
            entity = self._follow_merge(found)
            return self._observed(
                entity,
                method if method != ResolutionMethod.CREATED else ResolutionMethod.EXACT,
                proposed,
                effective,
                extra_aliases=extra_aliases,
                summary=summary,
                observed_at=observed_at,
                warnings=warnings,
                type_overridden=type_overridden,
            )

        # --- 4b. same name, different type: seeds and people win -----------------------------
        same_name = [e for e in self._store.find_by_name(normalized) if e.merged_into_id is None]
        for candidate in same_name:
            if candidate.type is effective:
                continue
            if candidate.engine is EngineKind.DETERMINISTIC:
                warnings.append(
                    f"kept the deterministic entity {candidate.canonical_name!r} "
                    f"({candidate.type.value}); refused to retype it to {effective.value}"
                )
                return self._observed(
                    candidate,
                    ResolutionMethod.SEED,
                    proposed,
                    candidate.type,
                    extra_aliases=extra_aliases,
                    summary=summary,
                    observed_at=observed_at,
                    warnings=warnings,
                    type_overridden=True,
                )
            if candidate.type is EntityType.PERSON and effective in PERSON_PROTECTED_FROM:
                warnings.append(
                    f"{canonical_name!r} is already a Person; refused to retype it to "
                    f"{effective.value} (ownership edges would be corrupted)"
                )
                return self._observed(
                    candidate,
                    ResolutionMethod.PERSON_REPAIR,
                    proposed,
                    EntityType.PERSON,
                    extra_aliases=extra_aliases,
                    summary=summary,
                    observed_at=observed_at,
                    warnings=warnings,
                    type_overridden=True,
                )

        # --- 5. trigram within the same type -------------------------------------------------
        candidates = self._store.trigram_candidates(normalized, effective, limit=5)
        strong = [(e, s) for e, s in candidates if s >= self._threshold]
        if strong:
            entity, similarity = strong[0]
            return self._observed(
                self._follow_merge(entity),
                ResolutionMethod.TRIGRAM,
                proposed,
                effective,
                extra_aliases=[*extra_aliases, raw],
                summary=summary,
                observed_at=observed_at,
                warnings=warnings,
                type_overridden=type_overridden,
                similarity=similarity,
            )

        # --- 6. LLM tie-break, off by default ------------------------------------------------
        ambiguous = [(e, s) for e, s in candidates if 0.55 <= s < self._threshold]
        if self._llm_tiebreak and self._provider is not None and ambiguous:
            picked = self._ask_model(raw, effective, ambiguous)
            if picked is not None:
                warnings.append("resolved by LLM tie-break (off by default)")
                return self._observed(
                    self._follow_merge(picked),
                    ResolutionMethod.LLM,
                    proposed,
                    effective,
                    extra_aliases=[*extra_aliases, raw],
                    summary=summary,
                    observed_at=observed_at,
                    warnings=warnings,
                    type_overridden=type_overridden,
                )

        if not create:
            return None

        # --- 7. create -----------------------------------------------------------------------
        entity = Entity(
            id=entity_id_for(effective, normalized, project_id),
            type=effective,
            canonical_name=canonical_name,
            normalized_name=normalized,
            aliases=sorted({a for a in [*extra_aliases, raw] if normalize_name(a) != normalized}),
            project_id=project_id,
            summary=summary,
            engine=self._engine,
            first_seen_at=observed_at,
            last_seen_at=observed_at,
        )
        stored = self._store.upsert(entity)
        logger.info(
            "entity.created",
            entity_id=str(stored.id),
            type=stored.type.value,
            name=stored.canonical_name,
            engine=stored.engine.value,
        )
        return ResolvedEntity(
            entity=stored,
            method=ResolutionMethod.CREATED,
            proposed_type=proposed,
            effective_type=effective,
            created=True,
            type_overridden=type_overridden,
            warnings=warnings,
        )

    # ---- helpers ---------------------------------------------------------------------------

    def _follow_merge(self, entity: Entity) -> Entity:
        """A merged loser keeps its row; resolution follows the pointer to the winner."""
        seen: set[UUID] = set()
        current = entity
        while current.merged_into_id is not None and current.merged_into_id not in seen:
            seen.add(current.id)
            winner = self._store.get(current.merged_into_id)
            if winner is None:
                break
            current = winner
        return current

    def _observed(
        self,
        entity: Entity,
        method: str,
        proposed: EntityType,
        effective: EntityType,
        *,
        extra_aliases: Sequence[str],
        summary: str | None,
        observed_at: datetime,
        warnings: list[str],
        type_overridden: bool = False,
        similarity: float | None = None,
    ) -> ResolvedEntity:
        """Refresh ``last_seen_at`` / aliases / summary on an existing row without changing its type."""
        new_aliases = sorted(
            {
                *entity.aliases,
                *[a for a in extra_aliases if normalize_name(a) != entity.normalized_name],
            }
        )
        updated = self._store.upsert(
            entity.model_copy(
                update={
                    "aliases": new_aliases,
                    "summary": entity.summary or summary,
                    "last_seen_at": observed_at,
                }
            )
        )
        return ResolvedEntity(
            entity=updated,
            method=method,
            proposed_type=proposed,
            effective_type=effective,
            created=False,
            type_overridden=type_overridden,
            similarity=similarity,
            warnings=warnings,
        )

    def _ask_model(
        self, name: str, entity_type: EntityType, candidates: Sequence[tuple[Entity, float]]
    ) -> Entity | None:
        """Genuinely ambiguous near-miss only. Never called unless ``llm_tiebreak=True``."""
        options = {str(i): entity for i, (entity, _) in enumerate(candidates)}
        listing = "\n".join(
            f"{i}: {entity.canonical_name} ({entity.type.value})" for i, entity in options.items()
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["choice"],
            "properties": {"choice": {"type": "string"}},
        }
        prompt = (
            f"Which of these existing {entity_type.value} entities is the same thing as "
            f'"{name}"?\n{listing}\n'
            'Answer with the number, or "none".'
        )
        try:
            response = self._provider.complete_json(prompt, schema)
        except Exception:  # noqa: BLE001 - a tie-break must never fail an episode
            return None
        parsed = getattr(response, "parsed", None) or {}
        return options.get(str(parsed.get("choice", "none")).strip())


class SqlEntityStore:
    """PostgreSQL :class:`EntityStore`. Never commits; the caller owns the transaction."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def get(self, entity_id: UUID) -> Entity | None:
        row = self._s.execute(
            text("SELECT * FROM entities WHERE id = :id"), {"id": str(entity_id)}
        ).mappings().first()
        return Entity(**dict(row)) if row is not None else None

    def find_exact(
        self, entity_type: EntityType, normalized_name: str, project_id: str | None
    ) -> Entity | None:
        row = self._s.execute(
            text(
                """
                SELECT * FROM entities
                 WHERE type = :type AND normalized_name = :name
                   AND coalesce(project_id, '') = coalesce(:project_id, '')
                 LIMIT 1
                """
            ),
            {
                "type": entity_type.value,
                "name": normalized_name,
                "project_id": project_id,
            },
        ).mappings().first()
        return Entity(**dict(row)) if row is not None else None

    def find_by_name(self, normalized_name: str) -> list[Entity]:
        rows = self._s.execute(
            text("SELECT * FROM entities WHERE normalized_name = :name ORDER BY first_seen_at"),
            {"name": normalized_name},
        ).mappings().all()
        return [Entity(**dict(row)) for row in rows]

    def trigram_candidates(
        self, normalized_name: str, entity_type: EntityType, *, limit: int = 5
    ) -> list[tuple[Entity, float]]:
        """``pg_trgm`` similarity **within one type**, strongest first, with the score kept.

        A08 needs the score (the >= 0.85 decision is ours, not the index's), which is why this does
        not reuse ``EntityRepo.trigram_candidates`` - that one returns rows without similarity and
        across all types.
        """
        rows = self._s.execute(
            text(
                """
                SELECT *, similarity(normalized_name, :name) AS score
                  FROM entities
                 WHERE type = :type
                   AND merged_into_id IS NULL
                   AND normalized_name % :name
                 ORDER BY score DESC
                 LIMIT :limit
                """
            ),
            {"name": normalized_name, "type": entity_type.value, "limit": limit},
        ).mappings().all()
        out: list[tuple[Entity, float]] = []
        for row in rows:
            data = dict(row)
            score = float(data.pop("score"))
            out.append((Entity(**data), score))
        return out

    def upsert(self, entity: Entity) -> Entity:
        from ...persistence.repositories import EntityRepo  # noqa: PLC0415 - avoid import cycle

        return EntityRepo(self._s).upsert(entity)

    def merge(self, loser_id: UUID, winner_id: UUID) -> None:
        """Record a merge. The loser row stays, so old provenance still resolves (plan section G)."""
        if loser_id == winner_id:
            return
        self._s.execute(
            text("UPDATE entities SET merged_into_id = :winner WHERE id = :loser"),
            {"winner": str(winner_id), "loser": str(loser_id)},
        )
        logger.info("entity.merged", loser=str(loser_id), winner=str(winner_id))
