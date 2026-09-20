"""``mcp_audit_log`` emission (ADR-0008). Owner: A10.

Every write call - allowed or refused - produces one audit record with the columns the table already
has: ``id, at, tool, kind, client_id, arguments, confirmed, allowed, denied_reason, result_ref,
latency_ms``. The refused ones matter most: "writes are off by default" is only checkable after the
fact if the refusal left a trace.

Where the row goes
------------------
The mcp-server holds no database credentials (ADR-0008), so it cannot INSERT. It posts the record to
memory-api, which owns the only connection to PostgreSQL, at ``POST /v1/mcp/audit``.

**That route does not exist yet.** ``schemas/api/openapi.json`` has 14 paths and none of them is an
audit sink, and ``apps/memory-api`` is A09's to change - see the NEEDS_HANDOFF in the P11-T01 result.
Until it lands this module degrades honestly rather than pretending: the endpoint is probed once, and
if it answers 404/405 the trail switches to ``sink="local"``, keeps every record in a bounded
in-process ring buffer, emits each one as a structured log line (``mcp.audit``), and reports
``degraded=True`` plus a ``dropped`` count on ``GET /health``. It never fails a tool call because the
audit could not be stored, and it never silently claims the row was persisted.

Nothing here retries. A retry queue would need durable storage this container is not allowed to have.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from aimemory.common.logging import get_logger
from api_client import ApiError, MemoryApiClient

__all__ = ["AuditRecord", "AuditTrail"]

logger = get_logger(__name__)

#: How many records to keep in memory when the Gateway sink is unavailable. Bounded on purpose:
#: this is a debugging aid and a /health signal, not a second store of record.
RING_SIZE = 200

#: After latching to the local sink, re-probe the gateway once every this many records, so a
#: memory-api that gains the route later is picked up without restarting the MCP server.
RESINK_PROBE_EVERY = 20


@dataclass(slots=True)
class AuditRecord:
    """One row of ``mcp_audit_log``. Field names match the columns exactly."""

    tool: str
    kind: str
    client_id: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    confirmed: bool = False
    allowed: bool = True
    denied_reason: str | None = None
    result_ref: str | None = None
    latency_ms: int | None = None
    id: UUID = field(default_factory=uuid4)
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["id"] = str(self.id)
        payload["at"] = self.at.isoformat()
        return payload


class AuditTrail:
    """Emits :class:`AuditRecord` s to memory-api, degrading to logs when the sink is absent."""

    def __init__(self, client: MemoryApiClient, *, enabled: bool = True) -> None:
        self._client = client
        self.enabled = enabled
        self.sink = "gateway"
        self.submitted = 0
        self.persisted = 0
        self.dropped = 0
        self._warned = False
        #: Submitted-count at which the sink was last latched to "local", so a periodic re-probe can
        #: recover from a latch that is no longer true (a redeploy that adds the route, say).
        self._latched_at = 0
        self._transient_failures = 0
        self.recent: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)

    @property
    def degraded(self) -> bool:
        return self.sink != "gateway"

    def stats(self) -> dict[str, Any]:
        return {
            "sink": self.sink,
            "degraded": self.degraded,
            "submitted": self.submitted,
            "persisted": self.persisted,
            "dropped": self.dropped,
        }

    async def record(self, entry: AuditRecord) -> None:
        """Best-effort persistence. Never raises - an audit failure must not fail a tool call."""
        if not self.enabled:
            return
        self.submitted += 1
        payload = entry.to_json()
        self.recent.append(payload)

        # A latch is only ever justified by "the route is not there". Re-probe periodically anyway,
        # so a memory-api redeploy that adds the route recovers without restarting this server.
        if self.sink == "local" and self.submitted - self._latched_at >= RESINK_PROBE_EVERY:
            self.sink = "gateway"
            self._latched_at = self.submitted

        if self.sink == "gateway":
            try:
                await self._client.post_audit(payload)
                if self._transient_failures or self._warned:
                    logger.info("mcp.audit_sink_recovered", dropped_while_degraded=self.dropped)
                self._transient_failures = 0
                self._warned = False
                self.persisted += 1
                return
            except ApiError as exc:
                if exc.status in (404, 405):
                    # The route genuinely is not served by the running memory-api. Latching is
                    # correct here - that is what the latch was for.
                    self.sink = "local"
                    self._latched_at = self.submitted
                    self._warn_once(
                        "mcp.audit_sink_missing",
                        "memory-api has no POST /v1/mcp/audit route; MCP audit records are being "
                        "written to the local structured log only (ADR-0008 handoff to A09).",
                        status=exc.status,
                    )
                else:
                    # Transient: a refused connection while memory-api restarts (status is None), or
                    # a 5xx. Latching on this was a real defect - MEASURED in the running server, one
                    # restart at 18:14 sent the sink to "local" permanently and 40 of 47 records,
                    # including every write refusal after that instant, never reached
                    # `mcp_audit_log`. ADR-0008's audit trail is the control that makes MCP writes
                    # safe to enable, so it must not be disarmed by a blip. Stay on the gateway and
                    # let the next record retry.
                    self._transient_failures += 1
                    self._warn_once(
                        "mcp.audit_sink_failed",
                        "memory-api did not accept the MCP audit record; keeping the gateway sink "
                        "and retrying on the next record.",
                        code=exc.code,
                        status=exc.status,
                    )

        self.dropped += 1
        # The record still exists somewhere a human can read it, and it is labelled as not persisted.
        logger.warning("mcp.audit", persisted=False, **_log_fields(payload))

    def _warn_once(self, event: str, message: str, **fields: Any) -> None:
        if self._warned:
            return
        self._warned = True
        logger.warning(event, message=message, **fields)


def _log_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten a record for structlog. ``arguments`` is already redacted by ``safeguards``."""
    return {
        "audit_id": payload["id"],
        "at": payload["at"],
        "tool": payload["tool"],
        "kind": payload["kind"],
        "client_id": payload["client_id"],
        "confirmed": payload["confirmed"],
        "allowed": payload["allowed"],
        "denied_reason": payload["denied_reason"],
        "result_ref": payload["result_ref"],
        "latency_ms": payload["latency_ms"],
        "arguments": payload["arguments"],
    }


class Stopwatch:
    """Millisecond timer for ``latency_ms``. MEASURED wall time around the tool body."""

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000)
