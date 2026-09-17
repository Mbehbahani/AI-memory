"""``/health`` and ``/metrics``. Owner: A09.

**Health is a liveness answer, not a quality score.** PostgreSQL is the system of record (ADR-0001),
so it is the only *required* dependency: if it is reachable the service returns 200, and Neo4j or the
embedding service being down shows up as ``status="degraded"`` with the failing component named. That
is deliberate - those two outages have defined degraded behaviours (vector+keyword, keyword-only), and
a container that marks itself unhealthy for a condition it is designed to survive would take the whole
compose stack down with it (mcp-server waits on this healthcheck).

``/metrics`` returns MEASURED counters since process start plus MEASURED corpus totals. No targets,
no estimates, no rates.
"""

from __future__ import annotations

from typing import Annotated

from aimemory.common.logging import get_logger
from aimemory.common.time import utc_now
from aimemory.gateway import Gateway, HealthReport, MetricsSnapshotView
from deps import get_gateway
from fastapi import APIRouter, Depends, Response
from metrics import COUNTERS

logger = get_logger(__name__)

router = APIRouter(tags=["system"])

GatewayDep = Annotated[Gateway, Depends(get_gateway)]


@router.get("/health", response_model=HealthReport, summary="Liveness of pg, neo4j and embedding")
def health(response: Response, gateway: GatewayDep) -> HealthReport:
    report = gateway.health()
    if not report.healthy:
        response.status_code = 503
    return report


@router.get("/metrics", response_model=MetricsSnapshotView, summary="In-process counters + corpus")
def metrics(gateway: GatewayDep) -> MetricsSnapshotView:
    snapshot = COUNTERS.snapshot()
    try:
        corpus = gateway.metrics_corpus()
    except Exception as exc:  # noqa: BLE001 - metrics must not fail while the database blinks
        logger.warning("memory_api.metrics_corpus_failed", error=type(exc).__name__)
        corpus = {}
    return MetricsSnapshotView(
        at=utc_now(),
        uptime_seconds=round(COUNTERS.uptime_seconds, 3),
        requests_total=snapshot["requests_total"],
        responses_total=snapshot["responses_total"],
        errors_total=snapshot["errors_total"],
        warnings_total=snapshot["warnings_total"],
        latency_ms=COUNTERS.mean_latency_ms(),
        corpus=corpus,
    )
