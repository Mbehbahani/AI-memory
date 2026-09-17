"""``aimemory.knowledge`` - the knowledge engine ADR-0009 selected, and its persistence.

This module is deliberately thin: it exists so that callers outside the knowledge layer -
:func:`aimemory.sources.tier2.run_tier2` above all - can ask for "the engine" and "the writer"
without importing an implementation directly and without knowing which one ADR-0009 chose.

Both factories are imported lazily inside the functions. Constructing the engine reaches
``aimemory.providers.llm``, which reaches back into configuration, so a module-level import here
would create a cycle; keeping it local also means that merely importing this package never requires
an LLM provider, credentials, or a database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from ..common.config import Settings
    from ..domain.models import Episode
    from ..domain.ports import ExtractionResult, KnowledgeEngine

__all__ = ["get_engine", "get_writer"]


def get_engine(settings: Settings | None = None) -> KnowledgeEngine:
    """The :class:`~aimemory.domain.ports.KnowledgeEngine` ADR-0009 selected.

    ADR-0009 measured Graphiti out (6-10 LLM calls per episode against the native engine's 2-3, and
    17-29 minutes per episode on the local provider) and settled on the native temporal engine;
    ``graphiti_engine`` remains an empty package as a documented placeholder. There is therefore one
    implementation to return, and no switch to make - if that ever changes, a superseding ADR decides
    it and this function is where the choice belongs.

    The engine builds its own :class:`~aimemory.domain.ports.LLMProvider` from ``LLM_PROVIDER``
    (ADR-0012/ADR-0014), so the caller does not choose a provider either.
    """
    from ..common.config import get_settings
    from .native_engine import NativeTemporalEngine

    return NativeTemporalEngine(settings=settings or get_settings())


def get_writer(scope: Any, *, graph: Any = None) -> Callable[[ExtractionResult, Episode], None]:
    """The ``ResultWriter`` that persists an :class:`ExtractionResult` under the ADR-0005 rules.

    ``scope`` is a session scope (``aimemory.sources.pipeline.SessionScope`` or a ``Database``); the
    returned closure opens one unit of work per episode, which is the transaction boundary the
    functional close-then-insert pair requires.
    """
    from .persist import make_result_writer

    return make_result_writer(scope, graph=graph)
