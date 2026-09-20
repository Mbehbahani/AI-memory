"""The MCP audit sink must survive a transient memory-api outage (ADR-0008).

Found by A13's P15 review, and MEASURED on the running server: `memory-api` was recreated at
18:14:52, `AuditTrail.record` caught the resulting `api_unreachable` and set `sink = "local"` - a
latch nothing ever cleared. From that instant on, **40 of 47 records, including every write refusal,
never reached `mcp_audit_log`** (`/health` reported `sink: "local", dropped: 40` while the table held
only the 7 written before the restart).

That matters more than a dropped log line: ADR-0008's audit trail is the control that makes enabling
MCP writes safe. A blip must not be able to disarm it. Latching is only ever justified by "the route
is genuinely not served" (404/405), never by "the service was briefly restarting".
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "apps" / "mcp-server"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

pytest.importorskip("mcp", reason="the mcp SDK is only installed in the api/mcp/tools images")

from api_client import ApiError
from audit import RESINK_PROBE_EVERY, AuditRecord, AuditTrail


class _Client:
    """A stand-in memory-api whose next response is scripted."""

    def __init__(self) -> None:
        self.raise_with: ApiError | None = None
        self.calls = 0

    async def post_audit(self, body):
        self.calls += 1
        if self.raise_with is not None:
            raise self.raise_with
        return {"stored": True, "id": body["id"]}


def _record(tool: str = "memory.add_episode") -> AuditRecord:
    return AuditRecord(tool=tool, kind="write", client_id="test", allowed=False,
                       denied_reason="write_disabled_mcp")


def _run(coro):
    return asyncio.run(coro)


def test_a_transient_failure_does_not_latch_the_sink() -> None:
    """The regression. A refused connection (status None) is not evidence the route is absent."""
    client = _Client()
    trail = AuditTrail(client)

    client.raise_with = ApiError("api_unreachable", status=None)
    _run(trail.record(_record()))
    assert trail.sink == "gateway", "a transient failure must not disarm the audit trail"
    assert trail.dropped == 1

    # The very next record retries and lands, without a restart.
    client.raise_with = None
    _run(trail.record(_record()))
    assert trail.persisted == 1
    assert trail.degraded is False


def test_a_server_error_also_does_not_latch() -> None:
    client = _Client()
    trail = AuditTrail(client)
    client.raise_with = ApiError("api_error", status=503)
    _run(trail.record(_record()))
    assert trail.sink == "gateway"

    client.raise_with = None
    _run(trail.record(_record()))
    assert trail.persisted == 1


@pytest.mark.parametrize("status", [404, 405])
def test_a_missing_route_does_latch(status: int) -> None:
    """Latching on a genuinely absent route is the behaviour the latch exists for: it stops one
    pointless HTTP call per tool invocation against a memory-api that cannot serve it."""
    client = _Client()
    trail = AuditTrail(client)
    client.raise_with = ApiError("api_error", status=status)
    _run(trail.record(_record()))
    assert trail.sink == "local"
    assert trail.degraded is True


def test_a_latched_sink_re_probes_so_a_redeploy_recovers() -> None:
    """A latch must not be permanent either: memory-api may gain the route on the next deploy, and
    the MCP server should not need restarting to notice."""
    client = _Client()
    trail = AuditTrail(client)
    client.raise_with = ApiError("api_error", status=404)
    _run(trail.record(_record()))
    assert trail.sink == "local"

    calls_while_latched = client.calls
    client.raise_with = None
    for _ in range(RESINK_PROBE_EVERY):
        _run(trail.record(_record()))

    assert client.calls > calls_while_latched, "a latched sink never re-probed the gateway"
    assert trail.persisted >= 1
    assert trail.sink == "gateway"


def test_an_audit_failure_never_raises_into_the_tool_call() -> None:
    """Whatever happens to the sink, the record call itself must not fail a tool invocation."""
    client = _Client()
    trail = AuditTrail(client)
    client.raise_with = ApiError("api_unreachable", status=None)
    _run(trail.record(_record()))  # must not raise
    assert trail.submitted == 1
