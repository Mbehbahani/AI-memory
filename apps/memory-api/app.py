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
from fastapi.responses import JSONResponse
from metrics import COUNTERS
from routes import ops, read, search, system, write

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



#: Methods that can change state, and so are worth protecting from a cross-origin trigger.
_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _allowed_origins(request: Request) -> frozenset[str]:
    """Origins treated as this service's own.

    Built from the request's own ``Host`` rather than hard-coded, so the service keeps working on a
    non-default port without a second setting to keep in sync. Both schemes are accepted because the
    page is served over plain HTTP on loopback.
    """
    host = request.headers.get("host", "")
    if not host:
        return frozenset()
    return frozenset({f"http://{host}", f"https://{host}"})


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
    async def reject_cross_origin_writes(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Refuse state-changing requests that carry a foreign ``Origin`` (SEC-02, A13's P15 review).

        The `/ops` page posts plain HTML forms with no CSRF token, and the same-origin policy does
        **not** stop a cross-origin form POST. A13 reproduced it: a page on another site could post
        ``action=scan&tier=2`` to `/ops/runs` and enqueue a run that the always-on worker executes -
        a paid Bedrock run triggered by a page the owner merely visited. Nothing could be read back
        (no CORS headers are sent), so this is a trigger, not a data leak - but triggering paid,
        state-changing work from a foreign page is enough.

        Why `Origin` and not a token: browsers attach `Origin` to every cross-origin state-changing
        request and cannot be talked out of it, which is exactly the threat here. Requests with **no**
        `Origin` are allowed through on purpose - that is server-to-server traffic (the MCP server
        calling this API over the compose network, `curl`, the test client), which a browser cannot
        forge. Adding a token would also mean session state, which this single-user local service
        deliberately does not have.
        """
        if request.method in _STATE_CHANGING_METHODS:
            origin = request.headers.get("origin")
            if origin and origin not in _allowed_origins(request):
                logger.warning(
                    "memory_api.cross_origin_write_rejected",
                    method=request.method,
                    path=request.url.path,
                    origin=origin[:200],
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "cross_origin_write_rejected",
                        "message": (
                            "State-changing requests from a different origin are refused. This API "
                            "is loopback-only and has no browser clients other than its own /ops page."
                        ),
                        "context": {"origin": origin[:200]},
                    },
                )
        return await call_next(request)

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
    application.include_router(ops.router)
    logger.info("memory_api.app_created", paths=len(application.openapi()["paths"]))
    return application


app = create_app()
