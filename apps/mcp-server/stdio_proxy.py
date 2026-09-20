"""stdio <-> streamable-HTTP proxy for MCP clients that can only spawn a subprocess. Owner: A10.

Plan section R asks for "streamable HTTP on 127.0.0.1:8020 plus a stdio launcher". This is the stdio
launcher. It is deliberately *not* a second server: a second server would be a second copy of the
ADR-0008 safeguards, a second rate-limit window and a second place for the tool list to drift. It is
a pump. Every byte a client writes on stdin is forwarded, unchanged, to the running mcp-server over
HTTP, and every response is written back to stdout - so a stdio client and an HTTP client hit exactly
the same gate, the same limiter and the same audit trail.

Run it through ``scripts/mcp-stdio.ps1`` / ``scripts/mcp-stdio.sh``, which invoke it inside the
mcp-server container so the host needs nothing but Docker::

    docker compose exec -T -w /app/apps/mcp-server mcp-server python stdio_proxy.py

``MCP_PROXY_URL`` (default ``http://localhost:8020/mcp``) points it at the server. Note that stdout is
the MCP channel: this module must never print to it. Diagnostics go to stderr.
"""

from __future__ import annotations

import os
import sys

import anyio
import httpx
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server

DEFAULT_URL = os.environ.get("MCP_PROXY_URL", "http://localhost:8020/mcp")

#: Long, because a proxied session is idle between a user's questions and an idle SSE read that
#: times out would tear down the session the client still believes it has.
SSE_READ_TIMEOUT = 600.0


def _log(message: str) -> None:
    print(f"[mcp-stdio] {message}", file=sys.stderr, flush=True)


async def _pump(source, sink, direction: str) -> None:  # type: ignore[no-untyped-def]
    """Forward messages one way until the source closes. Malformed input is reported, not fatal."""
    try:
        async for item in source:
            if isinstance(item, Exception):
                _log(f"{direction}: dropping an unreadable message ({type(item).__name__})")
                continue
            await sink.send(item)
    except anyio.ClosedResourceError:  # pragma: no cover - normal shutdown race
        pass
    except anyio.BrokenResourceError:  # pragma: no cover - the other end went away
        _log(f"{direction}: the connection closed")


async def run(url: str) -> None:
    _log(f"proxying stdio to {url}")
    timeout = httpx.Timeout(connect=5.0, read=SSE_READ_TIMEOUT, write=60.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        async with streamable_http_client(url, http_client=http) as (
            http_read,
            http_write,
            _session_id,
        ):
            async with stdio_server() as (stdin_read, stdout_write):
                async with anyio.create_task_group() as tg:

                    async def client_to_server() -> None:
                        await _pump(stdin_read, http_write, "client->server")
                        tg.cancel_scope.cancel()  # stdin closed: the client is gone

                    async def server_to_client() -> None:
                        await _pump(http_read, stdout_write, "server->client")
                        tg.cancel_scope.cancel()

                    tg.start_soon(client_to_server)
                    tg.start_soon(server_to_client)


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    url = args[0] if args else DEFAULT_URL
    try:
        anyio.run(run, url)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except Exception as exc:  # noqa: BLE001 - a launcher must explain itself before it dies
        _log(f"failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
