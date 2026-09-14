"""structlog configuration: JSON lines on stdout, with secret redaction.

Consumers: every service entry point (apps/ingestion, apps/memory-api, apps/mcp-server,
apps/embedding-service) calls :func:`configure_logging` once at startup; every module uses
:func:`get_logger`.

Two project rules are enforced here rather than by convention:

* **Nothing secret is ever logged** (plan §T). A processor redacts values whose key looks like a
  credential and rewrites anything matching a connection string with an inline password.
* **Errors are logged with their detail but rendered sanitized.** :class:`~aimemory.common.errors.
  AiMemoryError` instances are flattened into ``error`` / ``error_code`` / ``error_detail`` fields.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from .errors import AiMemoryError

__all__ = ["configure_logging", "get_logger", "redact_processor"]

_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|authorization|credential|dsn|database_url)",
    re.IGNORECASE,
)
_URL_CREDENTIALS_RE = re.compile(r"(?P<scheme>[a-z0-9+.\-]+://)(?P<user>[^:/\s]+):[^@/\s]+@")
REDACTED = "***redacted***"


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _URL_CREDENTIALS_RE.sub(r"\g<scheme>\g<user>:" + REDACTED + "@", value)
    if isinstance(value, dict):
        return {k: (REDACTED if _SECRET_KEY_RE.search(str(k)) else _redact_value(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: mask credential-looking keys and inline URL passwords."""
    for key in list(event_dict):
        if _SECRET_KEY_RE.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


def _error_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    exc = event_dict.pop("error_obj", None)
    if isinstance(exc, AiMemoryError):
        event_dict["error_code"] = exc.code
        event_dict["error"] = exc.public_message
        if exc.detail:
            event_dict["error_detail"] = exc.detail
        if exc.context:
            event_dict["error_context"] = exc.context
    elif exc is not None:
        event_dict["error_code"] = "internal_error"
        event_dict["error"] = type(exc).__name__
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog + stdlib logging. Idempotent; safe to call from every entry point.

    ``fmt="json"`` (``LOG_FORMAT=json``) emits one JSON object per line — the format the Ops page and
    ``docker compose logs`` parsing expect. ``fmt="console"`` is for interactive CLI use only.
    """
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=numeric, force=True)

    renderer: Any
    if fmt.lower() == "console":
        renderer = structlog.dev.ConsoleRenderer(colors=False)
    else:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _error_processor,
            redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger. Pass ``__name__`` from the calling module."""
    return structlog.get_logger(name)
