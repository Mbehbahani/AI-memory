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

#: Sentinel for ``get_writer(graph=...)``. ``None`` means "no projection, on purpose" (tests, and any
#: caller that wants PostgreSQL only); the default means "open one if Neo4j is there".
_AUTO_GRAPH: Any = object()


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
    from ..persistence.db import Database
    from .native_engine import NativeTemporalEngine
    from .telemetry import record_call

    resolved = settings or get_settings()

    def _sink(record: Any) -> None:
        """Persist one call's tokens, latency and price to `llm_calls`.

        Opens its own short transaction per call rather than joining the extraction transaction.
        That is deliberate: extraction rolls back on failure, and a *failed* call is exactly the one
        whose cost you most want recorded - it was paid for and produced nothing. Sharing the
        transaction would roll the evidence away with the work.
        """
        try:
            with Database(resolved).session() as session:
                record_call(session, record)
        except Exception:  # noqa: BLE001 - never break extraction to write a metric
            pass

    return NativeTemporalEngine(settings=resolved, call_sink=_sink)


def get_writer(scope: Any, *, graph: Any = _AUTO_GRAPH) -> Callable[[ExtractionResult, Episode], None]:
    """The ``ResultWriter`` that persists an :class:`ExtractionResult` under the ADR-0005 rules.

    ``scope`` is a session scope (``aimemory.sources.pipeline.SessionScope`` or a ``Database``); the
    returned closure opens one unit of work per episode, which is the transaction boundary the
    functional close-then-insert pair requires.

    **The graph is projected incrementally, and its absence is not an error.** By default this opens
    a Neo4j store so that a Tier 2 run keeps the projection current instead of leaving it stale until
    the next ``rebuild-graph``. If Neo4j is unreachable,
    :func:`~aimemory.providers.graph.factory.open_graph_store` logs ``graph.unavailable`` and returns
    ``None``: extraction then writes PostgreSQL - the system of record (ADR-0001) - and the graph is
    repaired later by a rebuild. Losing a rebuildable projection must never cost knowledge.

    Pass ``graph=None`` to disable projection deliberately, or a store/double to supply your own.
    """
    from .persist import make_result_writer

    if graph is _AUTO_GRAPH:
        from ..providers.graph.factory import open_graph_store

        graph = open_graph_store()

    return make_result_writer(scope, graph=graph)
