"""Process-wide dependencies for memory-api: one :class:`Gateway`, built once, closed on shutdown.

Owner: A09 (P10-T02). The FastAPI app is deliberately thin - it owns HTTP concerns only, and every
access rule lives in :mod:`aimemory.gateway`.

Construction is **fault-tolerant on purpose**. Neo4j or the embedding service being down at startup
must not stop memory-api from starting: the plan's failure tests require the API to answer in a
degraded mode (vector+keyword, or keyword-only) with a warning, and a process that refuses to boot
cannot do that. Each optional dependency is therefore built inside a try/except, its absence is
logged once, and ``/health`` reports it honestly for as long as it lasts.

Neo4j is opened with ``NEO4J_USER``/``NEO4J_PASSWORD`` as compose passes them - and compose passes
the ``memory_reader`` credentials to this service. ADR-0013 is explicit that on Community Edition
this is an *identity*, not a privilege boundary; the real constraint is that every read path goes
through ``GraphStore.query()``, which refuses write clauses.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from aimemory.common.config import Settings, get_settings
from aimemory.common.errors import ConfigurationError
from aimemory.common.logging import configure_logging, get_logger
from aimemory.gateway import Gateway
from aimemory.persistence.db import Database
from aimemory.retrieval.config import load_retrieval_config
from fastapi import Request

logger = get_logger(__name__)


@dataclass
class Runtime:
    """Everything the process owns. One instance, created by the lifespan handler."""

    settings: Settings
    database: Database
    gateway: Gateway
    graph: Any | None = None
    embedder: Any | None = None

    def close(self) -> None:
        for name, resource in (("neo4j", self.graph), ("embedding", self.embedder)):
            closer = getattr(resource, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                    logger.warning("memory_api.close_failed", component=name, error=type(exc).__name__)
        self.database.dispose()


def _build_graph(settings: Settings) -> Any | None:
    try:
        from aimemory.persistence.graph_store import Neo4jGraphStore

        return Neo4jGraphStore(
            settings.neo4j.uri,
            settings.neo4j.user,
            settings.neo4j.password.get_secret_value(),
            database=settings.neo4j.database,
        )
    except Exception as exc:  # noqa: BLE001 - graph down -> vector+keyword with a warning
        logger.warning("memory_api.neo4j_unavailable", error=type(exc).__name__)
        return None


def _build_embedder(settings: Settings) -> Any | None:
    try:
        from aimemory.providers.embedding.http import HttpEmbeddingProvider

        return HttpEmbeddingProvider(settings.embedding)
    except Exception as exc:  # noqa: BLE001 - embedder down -> keyword-only with a warning
        logger.warning("memory_api.embedding_unavailable", error=type(exc).__name__)
        return None


def build_runtime(settings: Settings | None = None) -> Runtime:
    """Build the process runtime. Only PostgreSQL is mandatory (ADR-0001: system of record)."""
    resolved = settings or get_settings()
    configure_logging(resolved.logging.level, resolved.logging.format)
    database = Database(resolved)
    graph = _build_graph(resolved)
    embedder = _build_embedder(resolved)
    config = load_retrieval_config()

    @contextmanager
    def session_scope() -> Iterator[Any]:
        with database.session() as session:
            yield session

    gateway = Gateway(
        session_scope,
        graph=graph,
        embedder=embedder,
        config=config,
        settings=resolved,
    )
    logger.info(
        "memory_api.runtime_ready",
        neo4j=graph is not None,
        embedding=embedder is not None,
        writes_enabled=resolved.gateway.write_enabled,
        retrieval_config_version=config.version,
    )
    return Runtime(
        settings=resolved, database=database, gateway=gateway, graph=graph, embedder=embedder
    )


def get_gateway(request: Request) -> Gateway:
    """FastAPI dependency: the process :class:`Gateway`, built once by the lifespan handler."""
    runtime: Runtime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - only reachable if the lifespan never ran
        raise ConfigurationError("The service is not initialised.")
    return runtime.gateway


def get_runtime(request: Request) -> Runtime:
    runtime: Runtime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover
        raise ConfigurationError("The service is not initialised.")
    return runtime
