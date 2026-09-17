"""memory-api - the Memory Gateway over REST (plan section Q). Owner: A09 (P10-T02).

ASGI entry point: ``uvicorn app:app`` with ``WORKDIR /app/apps/memory-api`` (the same shape as
``apps/embedding-service``). The module deliberately lives at the app-directory root rather than
inside a ``apps.memory_api`` package: the directory name on disk is ``apps/memory-api``, and a
hyphen cannot appear in a Python module path - which is exactly the ``ModuleNotFoundError: No module
named 'apps.memory_api'`` the placeholder CMD produced before this file existed.

What this module owns and nothing else does:

* the **lifespan**: build one :class:`~deps.Runtime` (database + gateway + optional graph/embedder)
  at startup and close it at shutdown;
* the **counter middleware** feeding ``/metrics``;
* the **sanitized error handlers** (plan section T);
* the **router mounts**. P12-T02 (A16) adds ``routes/ops.py`` next to these and mounts it here.

Binding (ADR-0007): the published port is ``127.0.0.1:8000`` in ``docker-compose.yml``; that is the
loopback guarantee and ``tests/integration/test_gateway_api.py`` asserts it against the compose file.
Inside the container uvicorn binds :data:`DEFAULT_BIND_HOST` from ``MEMORY_API_BIND_HOST`` - the
Dockerfile sets it to the container's own interfaces, because a process bound to the container's
loopback is unreachable both from the published port (docker-proxy dials the container IP) and from
mcp-server. Outside a container the default stays ``127.0.0.1``.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aimemory.common.logging import get_logger
from deps import Runtime, build_runtime
from errors import install_error_handlers
from fastapi import FastAPI, Request, Response
from metrics import COUNTERS
from routes import read, search, system, write

__all__ = ["DEFAULT_BIND_HOST", "app", "create_app"]

logger = get_logger(__name__)

#: Loopback unless the runtime explicitly says otherwise (the container does; see the docstring).
DEFAULT_BIND_HOST = os.environ.get("MEMORY_API_BIND_HOST", "127.0.0.1")

API_TITLE = "AI Memory Gateway"
API_VERSION = "0.1.0"
API_DESCRIPTION = (
    "Local personal memory infrastructure (V0.1). Hybrid retrieval over a personal corpus with "
    "provenance on every returned object, point-in-time reads, and append-only writes that are "
    "disabled by default (ADR-0008). Published on 127.0.0.1 only (ADR-0007)."
)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """One runtime per process. A missing optional dependency logs and degrades; it never blocks."""
    runtime: Runtime = build_runtime()
    application.state.runtime = runtime
    try:
        yield
    finally:
        runtime.close()
        application.state.runtime = None


def create_app() -> FastAPI:
    """Build the ASGI application. Tests call this with their own runtime injected afterwards."""
    application = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        lifespan=lifespan,
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )

    @application.middleware("http")
    async def count_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        response: Response = await call_next(request)
        # The matched route *template* keeps the counter cardinality bounded: one key per route,
        # not one per entity id.
        matched = request.scope.get("route")
        path = getattr(matched, "path", None) or request.url.path
        label = f"{request.method} {path}"
        COUNTERS.record_request(label)
        COUNTERS.record_response(
            label, response.status_code, (time.perf_counter() - started) * 1000
        )
        return response

    install_error_handlers(application)
    application.include_router(system.router)
    application.include_router(search.router)
    application.include_router(read.router)
    application.include_router(write.router)
    logger.info("memory_api.app_created", paths=len(application.openapi()["paths"]))
    return application


app = create_app()
