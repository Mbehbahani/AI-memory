"""The ADR-0008 write gate: flags, confirm, rate limit, size limit, client tagging. Owner: A10.

ADR-0008 in one paragraph: ``memory.add_episode`` and ``memory.record_decision`` exist but are
disabled unless **both** ``MCP_WRITE_ENABLED=true`` and ``GATEWAY_WRITE_ENABLED=true``; each call
requires ``confirm=true``, is rate-limited to 10/min, size-limited to 8,000 characters, tagged with
the client id and logged to ``mcp_audit_log``; writes only ever append.

Two flags, two processes. ``MCP_WRITE_ENABLED`` is read from this container's environment.
``GATEWAY_WRITE_ENABLED`` belongs to memory-api and is deliberately *not* wired into this container -
so the gate reads it from ``GET /health`` (``writes_enabled``) with a short TTL rather than from a
duplicated env var that could drift out of step with the process that actually enforces it. The
consequence worth stating plainly: even if this module were wrong, memory-api refuses the write with
a 403 of its own, because A09 put the same check inside the Gateway service rather than in a route.

Check order, and why
--------------------
1. **Rate limit** - cheapest, and it must protect a *disabled* endpoint too, or "writes are off"
   becomes an invitation to hammer the server for free. Every attempt counts, allowed or not.
2. **MCP_WRITE_ENABLED** - this container's own posture. While it is false nothing else is worth
   saying, so this is the reason a default installation always gives.
3. **confirm=true** - an explicit act, never inferred.
4. **size limit** - checked here, before the frozen-schema validation, so an oversized body is
   audited as ``size_limit_exceeded`` rather than as a generic argument error.
5. **GATEWAY_WRITE_ENABLED** - the *other* service's posture, and deliberately last: once an operator
   has turned MCP writes on, "your payload is wrong" is more useful than "the other flag is off",
   and nothing is risked by the ordering because memory-api enforces that flag itself (403 from
   :class:`~aimemory.common.errors.WriteDisabledError`, inside the Gateway rather than in a route).
   The practical benefit is that confirm, size and rate limiting can all be exercised for real
   against a container with only MCP writes on - which cannot touch the corpus.

Every refusal returns a :class:`Denial` carrying a stable ``reason`` (the ``denied_reason`` column)
and a sentence safe to hand to a client. Nothing here raises for a refusal: a refusal is a normal,
audited outcome, not an exception.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from aimemory.common.logging import get_logger

__all__ = [
    "Denial",
    "RateLimiter",
    "WriteGate",
    "redact_arguments",
]

logger = get_logger(__name__)

#: Argument values longer than this are replaced by a length marker before they reach an audit row.
#: mcp_audit_log must stay a record of *what was attempted*, not a second copy of the payload.
AUDIT_VALUE_CHARS = 120


@dataclass(frozen=True, slots=True)
class Denial:
    """A refused call. ``reason`` goes into ``mcp_audit_log.denied_reason`` verbatim."""

    reason: str
    message: str


class RateLimiter:
    """Sliding-window limiter, ``limit`` events per ``window`` seconds, keyed per client id.

    In-process and per-container on purpose: there is exactly one mcp-server in V0.1, and a shared
    store would mean either a database credential (forbidden by ADR-0008) or a new dependency.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check_and_record(self, key: str, *, now: float | None = None) -> bool:
        """Record an attempt. Returns False when it exceeds the window budget."""
        moment = time.monotonic() if now is None else now
        events = self._events[key]
        cutoff = moment - self.window
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(moment)
        return True

    def remaining(self, key: str, *, now: float | None = None) -> int:
        moment = time.monotonic() if now is None else now
        events = self._events[key]
        cutoff = moment - self.window
        while events and events[0] <= cutoff:
            events.popleft()
        return max(0, self.limit - len(events))


class WriteGate:
    """Applies the ADR-0008 checks to one write call.

    ``gateway_writes_enabled`` is supplied by the caller (from the cached memory-api health probe) so
    this class stays pure and unit-testable: no clock it does not own, no network.
    """

    def __init__(
        self,
        *,
        mcp_write_enabled: bool,
        rate_limit_per_minute: int,
        max_text_chars: int,
        confirm_required: bool = True,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.mcp_write_enabled = mcp_write_enabled
        self.max_text_chars = max_text_chars
        self.confirm_required = confirm_required
        self.limiter = limiter or RateLimiter(rate_limit_per_minute)

    #: The argument of each write tool that carries free text subject to the size limit.
    TEXT_FIELDS: dict[str, tuple[str, ...]] = {
        "memory.add_episode": ("text",),
        "memory.record_decision": ("title", "statement"),
    }

    def evaluate(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        client_id: str,
        gateway_writes_enabled: bool,
    ) -> Denial | None:
        """Return the first refusal, or ``None`` when the call may proceed to memory-api."""
        if not self.limiter.check_and_record(client_id):
            return Denial(
                "rate_limited",
                f"Rate limit reached: at most {self.limiter.limit} write calls per minute per "
                "client. Try again shortly.",
            )

        if not self.mcp_write_enabled:
            return Denial(
                "write_disabled_mcp",
                "MCP writes are disabled. Set MCP_WRITE_ENABLED=true on the mcp-server container "
                "(and GATEWAY_WRITE_ENABLED=true on memory-api) to enable them. See "
                "docs/operations/mcp.md.",
            )
        if self.confirm_required and arguments.get("confirm") is not True:
            return Denial(
                "confirm_required",
                "This tool writes to memory and needs an explicit confirm=true.",
            )

        for field in self.TEXT_FIELDS.get(tool, ()):
            value = arguments.get(field)
            if isinstance(value, str) and len(value) > self.max_text_chars:
                return Denial(
                    "size_limit_exceeded",
                    f"'{field}' is {len(value)} characters; the limit is {self.max_text_chars}.",
                )

        if not gateway_writes_enabled:
            return Denial(
                "write_disabled_gateway",
                "The memory Gateway has writes disabled. Set GATEWAY_WRITE_ENABLED=true on the "
                "memory-api container. See docs/operations/mcp.md.",
            )
        return None


def redact_arguments(arguments: dict[str, Any], *, limit: int = AUDIT_VALUE_CHARS) -> dict[str, Any]:
    """Shrink an argument dict to something safe and small enough to store in an audit row.

    Long strings become ``"<1234 chars>"``. The *shape* of the call is preserved (which arguments
    were supplied, and the short scalar ones verbatim), because that is what an audit reader needs;
    the free text itself is already in the episode or artifact the write created, and does not
    belong in a second table.
    """

    def shrink(value: Any) -> Any:
        if isinstance(value, str):
            return value if len(value) <= limit else f"<{len(value)} chars>"
        if isinstance(value, list):
            return [shrink(v) for v in value[:20]]
        if isinstance(value, dict):
            return {k: shrink(v) for k, v in list(value.items())[:20]}
        return value

    return {key: shrink(value) for key, value in arguments.items()}
