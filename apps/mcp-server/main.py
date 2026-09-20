"""Process entrypoint for the mcp-server container. Owner: A10.

Run it the way the image does::

    uvicorn main:app --host 0.0.0.0 --port 8020     # from WORKDIR /app/apps/mcp-server

``apps/mcp-server`` contains a hyphen, so it can never be a Python package and
``python -m apps.mcp_server.main`` can never import - that placeholder CMD is what kept this
container in a restart loop before P11-T01. The fix is the one A09 used for memory-api: make the app
directory the working directory and import top-level modules from it.

**Binding.** Uvicorn binds ``0.0.0.0`` *inside* the container and ``docker-compose.yml`` publishes
``127.0.0.1:8020:8020``. The loopback guarantee (plan section T) is the published port, not the
in-container bind: binding the container's own 127.0.0.1 makes the service unreachable through
docker-proxy. Same convention as memory-api; ``tests/`` asserts no service publishes off-loopback.

``MCP_TRANSPORT=stdio`` runs the same server over stdio instead, for a client that cannot speak HTTP
and does not want the proxy in ``scripts/mcp-stdio.*``.
"""

from __future__ import annotations

import os
import sys

from aimemory.common.config import get_settings
from aimemory.common.logging import configure_logging, get_logger
from server import build_server

configure_logging()
logger = get_logger(__name__)

#: 0.0.0.0 inside the container; the published port carries the loopback guarantee. Overridable so a
#: host-side run (`python main.py` outside Docker) can bind 127.0.0.1 instead.
BIND_HOST = os.environ.get("MCP_BIND_HOST", "0.0.0.0")  # noqa: S104 - see the module docstring


def create_app():  # type: ignore[no-untyped-def]
    """Build the Starlette app that serves MCP streamable-HTTP at ``/mcp`` plus ``GET /health``."""
    settings = get_settings()
    server = build_server()
    logger.info(
        "mcp.starting",
        transport="streamable-http",
        port=settings.mcp.host_port,
        gateway=settings.gateway.url,
        tools=len(server._tool_manager.list_tools()),
        write_enabled=settings.mcp.write_enabled,
    )
    return server.streamable_http_app()


#: Module-level ASGI app, so the container command is a plain ``uvicorn main:app``.
app = create_app()


def main() -> int:
    settings = get_settings()
    if settings.mcp.transport == "stdio":
        logger.info("mcp.starting", transport="stdio")
        build_server().run(transport="stdio")
        return 0

    import uvicorn

    uvicorn.run(
        app,
        host=BIND_HOST,
        port=settings.mcp.host_port,
        log_level=settings.logging.level.lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
