"""``aimemory.knowledge.native_engine`` - the engine ADR-0009 selected (owner A08, P8-T02).

2-3 schema-constrained LLM calls per episode against the frozen ``schemas/extraction/*.json``,
through whichever :class:`~aimemory.domain.ports.LLMProvider` ``LLM_PROVIDER`` names. It writes
nothing itself: :mod:`aimemory.knowledge.persist` resolves entities, applies the ADR-0005 temporal
rules, writes PostgreSQL and projects Neo4j.
"""

from .engine import MAX_BODY_CHARS, NativeTemporalEngine
from .schemas import episode_schema, relationship_schema

__all__ = [
    "MAX_BODY_CHARS",
    "NativeTemporalEngine",
    "episode_schema",
    "relationship_schema",
]
