"""``aimemory.knowledge.entity_resolution`` - deterministic-first resolution (owner A08, P8-T03).

``normalize -> alias tables -> deterministic seed guard -> exact -> pg_trgm >= 0.85 -> (LLM
tie-break, off) -> create``. The model can add entities; it can never retype one the system already
knows, because P4-T02 MEASURED both providers mistyping the same things.
"""

from .aliases import AliasHit, AliasIndex, load_alias_index
from .resolver import (
    PERSON_PROTECTED_FROM,
    TRIGRAM_THRESHOLD,
    EntityResolver,
    EntityStore,
    ResolutionMethod,
    ResolvedEntity,
    SqlEntityStore,
    entity_id_for,
)

__all__ = [
    "PERSON_PROTECTED_FROM",
    "TRIGRAM_THRESHOLD",
    "AliasHit",
    "AliasIndex",
    "EntityResolver",
    "EntityStore",
    "ResolutionMethod",
    "ResolvedEntity",
    "SqlEntityStore",
    "entity_id_for",
    "load_alias_index",
]
