"""Opening the Neo4j projection store without ever making it a hard dependency (A08).

ADR-0001 says PostgreSQL is the system of record and Neo4j is a *rebuildable projection*. The
practical consequence is this module: every caller that wants to project must be able to ask for a
store and be told "no" without that ending the run.

* :func:`open_graph_store` returns ``None`` - it does not raise - when Neo4j is unreachable,
  misconfigured, or unhealthy. A Tier 2 extraction then writes PostgreSQL and skips the projection;
  ``aimemory-ingest rebuild-graph`` repairs the graph afterwards.
* Pass ``required=True`` (the rebuild driver does) to get the exception instead: there, "Neo4j is
  down" means the rebuild did not happen and the caller must know.

The store owns a driver and therefore a socket. A caller that opens one with ``required=False`` and
keeps it for the length of a run is expected to :meth:`~Neo4jGraphStore.close` it; the ingestion CLI
is a short-lived process, so leaking it to interpreter shutdown is also acceptable there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...common.errors import GraphStoreError
from ...common.logging import get_logger
from ...persistence.graph_store import Neo4jGraphStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...common.config import Settings

__all__ = ["open_graph_store"]

logger = get_logger(__name__)


def open_graph_store(
    settings: Settings | None = None,
    *,
    required: bool = False,
    check_health: bool = True,
) -> Neo4jGraphStore | None:
    """Construct a :class:`Neo4jGraphStore`, or ``None`` when the graph is not usable.

    ``check_health`` runs one ``RETURN 1`` round-trip, because the ``neo4j`` driver is lazy: building
    it succeeds even when nothing is listening, and the failure would otherwise surface much later,
    inside a write, as a swallowed projection error per episode.
    """
    from ...common.config import get_settings  # noqa: PLC0415 - avoid an import cycle at module load

    resolved = settings or get_settings()
    store: Neo4jGraphStore | None = None
    try:
        store = Neo4jGraphStore(
            resolved.neo4j.uri,
            resolved.neo4j.user,
            resolved.neo4j.password.get_secret_value(),
            database=resolved.neo4j.database,
        )
        if check_health and not store.health():
            raise GraphStoreError(
                "Neo4j accepted a connection but reported unhealthy.",
                detail=f"uri={resolved.neo4j.uri}",
            )
    except Exception as exc:  # noqa: BLE001 - deliberate: the graph is optional (ADR-0001)
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001, S110 - closing a broken driver must not mask the cause
                pass
        if required:
            raise
        logger.warning(
            "graph.unavailable",
            uri=resolved.neo4j.uri,
            error=f"{type(exc).__name__}: {exc}",
            consequence="knowledge is written to PostgreSQL only; run rebuild-graph to repair",
        )
        return None
    return store
