"""``aimemory.knowledge.graphiti_engine`` - a **documented extension point, not an implementation**.

ADR-0009 (accepted 2026-09-15) decided against Graphiti for V0.1 and instructed that this package be
kept rather than deleted, so a future version can revisit the question cheaply. Nothing here is
wired into the runtime, ``graphiti-core`` is an optional extra and enters no runtime image, and
:func:`aimemory.knowledge.get_engine` never returns this class.

Why the decision went the way it did, in the order the ADR weights it:

1. **Provider independence.** The native engine calls whichever
   :class:`~aimemory.domain.ports.LLMProvider` is configured, so it works both online (Bedrock, the
   ADR-0014 default) and offline (Ollama). Graphiti-on-Bedrock would work with exactly one and would
   leave the offline mode with no extraction engine at all.
2. **Call budget.** Native extraction is 2-3 calls per episode (plan section M); Graphiti performs
   6-10. MEASURED in P4-T02: ``qwen3:4b`` median 174.43 s per extraction call, so a Graphiti episode
   on the local model costs 1,047-1,744 s against criterion C2's ``median <= 240 s`` - a failure by
   arithmetic, 4.4x to 7.3x over.
3. **No persistence saving.** ADR-0001 requires mirroring any engine's output into PostgreSQL so
   PostgreSQL stays the system of record and Neo4j stays rebuildable, so Graphiti was never going to
   save the work that dominates this phase.

The gate's ``graphiti-core`` / Anthropic SDK incompatibility is **not** a reason. It is a fixable
packaging problem and citing it would misrepresent why the decision was taken.

**Reopening condition (ADR-0009):** hardware on which a single extraction call is under ~25 s locally
would put 6-10 calls inside C2's 240 s budget and make the question live again. That would be a
superseding ADR, not an edit to this file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from ...domain.enums import EngineKind

__all__ = ["ADR_REFERENCE", "GraphitiEngine"]

ADR_REFERENCE = "docs/adr/ADR-0009-knowledge-engine-verdict.md"

_MESSAGE = (
    "The Graphiti engine is not implemented in V0.1. ADR-0009 selected the native temporal engine "
    "(aimemory.knowledge.native_engine.NativeTemporalEngine) and dropped Graphiti entirely, "
    "including the unmeasured Bedrock branch. This package is kept as a documented extension point "
    f"on ADR-0009's instruction. See {ADR_REFERENCE}."
)


class GraphitiEngine:
    """Stub :class:`~aimemory.domain.ports.KnowledgeEngine`. Every method raises.

    It exists so the seam ADR-0002 created stays visible in the code, and so a future attempt starts
    from a named place with the decision attached, instead of from a blank directory.
    """

    kind = EngineKind.GRAPHITI

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(_MESSAGE)

    def process_episode(self, episode: Any, context: Any) -> Any:
        raise NotImplementedError(_MESSAGE)

    def invalidate(self, fact_id: UUID, at: datetime, by_episode: UUID | None = None) -> None:
        raise NotImplementedError(_MESSAGE)
