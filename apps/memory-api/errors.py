"""Sanitized HTTP error responses (plan section T). Owner: A09.

Plan section T requires that nothing leaving the process contains a host path, a connection string, a
password, or content from a file flagged as holding secrets. :mod:`aimemory.common.errors` already
splits every error into a ``public_message`` and a local-only ``detail``; this module is the place
that guarantees only the first one is ever serialized.

Three handlers, one rule:

* :class:`~aimemory.common.errors.AiMemoryError` -> its own ``http_status`` and ``sanitized()``
  payload (``{"error": code, "message": ..., "context": {...}}``);
* ``RequestValidationError`` -> 422 with pydantic's field errors (which describe the *request*, not
  the server, so they are safe);
* anything else -> 500 with a fixed message and **no** exception text. The full traceback goes to the
  local structured log, where :mod:`aimemory.common.logging` redacts it.
"""

from __future__ import annotations

from typing import Any

from aimemory.common.errors import AiMemoryError
from aimemory.common.logging import get_logger
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from metrics import COUNTERS

logger = get_logger(__name__)

__all__ = ["install_error_handlers"]

GENERIC_500 = {
    "error": "internal_error",
    "message": "An internal error occurred.",
    "context": {},
}


def _log_and_count(request: Request, code: str, detail: str | None) -> None:
    COUNTERS.record_error(code)
    logger.warning(
        "memory_api.error",
        code=code,
        path=request.url.path,
        method=request.method,
        detail=detail,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Attach the three handlers. Called once by :func:`app.create_app`."""

    @app.exception_handler(AiMemoryError)
    async def _domain_error(request: Request, exc: AiMemoryError) -> JSONResponse:
        _log_and_count(request, exc.code, exc.detail)
        return JSONResponse(status_code=exc.http_status, content=exc.sanitized())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        _log_and_count(request, "validation_error", None)
        errors: list[dict[str, Any]] = [
            {"loc": [str(part) for part in error.get("loc", ())], "msg": str(error.get("msg", ""))}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": "The request did not match the expected shape.",
                "context": {},
                "errors": errors,
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # exc_info goes to the local log only; the response body never carries it.
        _log_and_count(request, "internal_error", type(exc).__name__)
        logger.error("memory_api.unhandled", path=request.url.path, error=type(exc).__name__)
        return JSONResponse(status_code=500, content=GENERIC_500)
