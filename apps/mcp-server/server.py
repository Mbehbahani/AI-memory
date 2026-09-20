"""FastMCP assembly: contract-driven tools, resources, safeguards, audit, ``/health``. Owner: A10.

Plan section R, ADR-0008. The server exposes exactly what ``schemas/mcp/tools.json`` says it does -
no more, no fewer, and with that file's ``inputSchema`` objects advertised verbatim.

Why the tools are built rather than decorated
---------------------------------------------
FastMCP's ``@mcp.tool()`` derives the advertised ``inputSchema`` from the handler's Python signature.
That is convenient and it is the wrong source of truth here: the contract is frozen (A02, ADR
required to change it) and P11-T02 asserts that the served list *equals* the file. A schema
re-derived from a signature would be equal only by luck, and would drift the first time someone added
a default. So each tool is constructed with the contract's schema object as ``parameters``, and
:class:`ContractTool` overrides ``run`` to validate the incoming arguments against that same object
(:mod:`validation`) before dispatching. One source of truth, used for both advertising and
enforcement.

``annotations`` are deliberately left unset. ``tools.json`` does not specify them, and an absent
annotation is the *safe* default: an MCP client treats an unannotated tool as potentially
destructive, which is the correct reading of a write tool. Adding ``readOnlyHint`` would be a
contract change and belongs to A02.

What every call goes through
----------------------------
``client id`` -> frozen-schema validation -> (writes only) the ADR-0008 gate -> the handler ->
``mcp_audit_log``. Refusals are audited with a ``denied_reason`` and returned as an MCP tool error;
they are never exceptions that escape as a traceback (plan section T).
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aimemory.common.config import get_settings
from aimemory.common.logging import get_logger
from aimemory.common.time import utc_now
from api_client import ApiError, MemoryApiClient
from audit import AuditRecord, AuditTrail, Stopwatch
from handlers import Handlers
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.server.fastmcp.exceptions import ToolError
from spec import Spec, ToolSpec, get_spec
from starlette.requests import Request
from starlette.responses import JSONResponse
from validation import SchemaViolation, assert_supported, validate_arguments

__all__ = ["ServerState", "build_server"]

logger = get_logger(__name__)

SERVER_NAME = "ai-memory"

#: How long a ``GET /health`` answer from memory-api is trusted before it is fetched again. Short
#: enough that flipping GATEWAY_WRITE_ENABLED takes effect without restarting mcp-server, long
#: enough that a burst of writes does not become a burst of health probes.
GATEWAY_HEALTH_TTL_SECONDS = 15.0

#: Audit every read call as well as every write. Off by default: ADR-0008 requires writes to be
#: audited, reads are already recorded in ``retrieval_logs`` by the Gateway, and a row per read
#: would make the table mostly noise. Set MCP_AUDIT_READS=true to record everything.
AUDIT_READS = os.environ.get("MCP_AUDIT_READS", "false").strip().lower() in {"1", "true", "yes"}


@dataclass
class GatewayStatus:
    """Cached answer to "does memory-api currently allow writes?"."""

    writes_enabled: bool = False
    status: str = "unknown"
    checked_at: float = 0.0
    reachable: bool = False


class ServerState:
    """Everything a handler needs, built once per process and hung off the FastMCP instance."""

    def __init__(self, *, spec: Spec | None = None, client: MemoryApiClient | None = None) -> None:
        from safeguards import WriteGate  # local import keeps the module import graph shallow

        settings = get_settings()
        self.spec = spec or get_spec()
        self.api = client or MemoryApiClient()
        self.handlers = Handlers(self.api)
        self.audit = AuditTrail(self.api)
        self.mcp_write_enabled = settings.mcp.write_enabled
        self.gate = WriteGate(
            mcp_write_enabled=self.mcp_write_enabled,
            rate_limit_per_minute=self.spec.rate_limit_per_minute,
            max_text_chars=self.spec.max_text_chars,
            confirm_required=self.spec.confirm_required,
        )
        self._gateway = GatewayStatus()
        self.calls_total = 0
        self.errors_total = 0
        self.refusals_total = 0

    async def gateway_status(self, *, force: bool = False) -> GatewayStatus:
        import time

        now = time.monotonic()
        if not force and now - self._gateway.checked_at < GATEWAY_HEALTH_TTL_SECONDS:
            return self._gateway
        try:
            report = await self.api.health()
            self._gateway = GatewayStatus(
                writes_enabled=bool(report.get("writes_enabled", False)),
                status=str(report.get("status", "unknown")),
                checked_at=now,
                reachable=True,
            )
        except ApiError as exc:
            logger.warning("mcp.gateway_health_failed", code=exc.code, status=exc.status)
            self._gateway = GatewayStatus(
                writes_enabled=False, status="unreachable", checked_at=now, reachable=False
            )
        return self._gateway

    async def aclose(self) -> None:
        await self.api.aclose()


# ==================================================================================== client id ==


def client_id_for(context: Context[Any, Any, Any] | None) -> str:
    """Identify the caller for tagging and rate limiting (ADR-0008 "tagged with the client id").

    Preference order: the ``clientInfo`` an MCP client sends at ``initialize`` (the honest answer,
    and the one Claude Code / Claude Desktop supply), then an ``X-MCP-Client`` header for a plain
    HTTP caller, then ``"unknown"``. The value is truncated and stripped of control characters -
    it is client-supplied and ends up in a database column.
    """
    name: str | None = None
    version: str | None = None
    if context is not None:
        try:
            params = context.session.client_params
        except Exception:  # noqa: BLE001 - no session (direct call, stdio handshake in flight)
            params = None
        info = getattr(params, "clientInfo", None)
        if info is not None:
            name = getattr(info, "name", None)
            version = getattr(info, "version", None)
        if not name:
            request = getattr(getattr(context, "request_context", None), "request", None)
            headers = getattr(request, "headers", None)
            if headers is not None:
                name = headers.get("x-mcp-client")
    if not name:
        return "unknown"
    label = f"{name}/{version}" if version else str(name)
    cleaned = "".join(ch for ch in label if ch.isprintable())
    return cleaned[:120]


# ================================================================================ contract tool ==


async def _schema_only() -> dict[str, Any]:  # pragma: no cover - never called
    """Placeholder body. Exists solely so ``Tool.from_function`` can build ``fn_metadata``."""
    return {}


class ContractTool(Tool):
    """A FastMCP tool whose advertised schema *is* the frozen contract, and whose ``run`` enforces it."""

    async def run(
        self,
        arguments: dict[str, Any],
        context: Context[Any, Any, Any] | None = None,
        convert_result: bool = False,
    ) -> Any:
        pipeline: Callable[..., Awaitable[Any]] = self.fn  # set to a bound _invoke by build_server
        result = await pipeline(arguments, context)
        if convert_result:
            result = self.fn_metadata.convert_result(result)
        return result


def _build_tool(spec: ToolSpec, pipeline: Callable[..., Awaitable[Any]]) -> ContractTool:
    schema = spec.input_schema
    assert_supported(schema, where=spec.name)
    base = Tool.from_function(_schema_only, name=spec.name, structured_output=False)
    return ContractTool(
        fn=pipeline,
        name=spec.name,
        title=spec.title,
        description=spec.description,
        parameters=schema,
        fn_metadata=base.fn_metadata,
        is_async=True,
        context_kwarg=None,
        annotations=None,
    )


# ====================================================================================== server ===


def build_server(state: ServerState | None = None) -> FastMCP:
    """Construct the FastMCP server. Pure assembly - no network call happens here."""
    resolved = state or ServerState()
    spec = resolved.spec
    settings = get_settings()

    mcp = FastMCP(
        SERVER_NAME,
        instructions=(
            "Personal AI memory for this machine: hybrid search over ingested sources, a project "
            "registry, resolved entities and facts with validity windows, decision artifacts and "
            "full provenance for every answer. Read tools return logical source URIs and citations, "
            "never file contents. The two write tools append only, require confirm=true, and are "
            "disabled unless the operator has enabled MCP and Gateway writes."
        ),
        host=os.environ.get("MCP_BIND_HOST", "0.0.0.0"),  # noqa: S104 - see main.py
        port=settings.mcp.host_port,
        # Stateful sessions (the default) on purpose: the client id used for tagging and rate
        # limiting comes from the ``clientInfo`` sent at ``initialize``, and a stateless transport
        # throws that away between requests. ``json_response`` keeps single-shot calls a plain
        # JSON POST response instead of an SSE stream, which is easier to curl and to test.
        json_response=True,
    )
    mcp.aimemory_state = resolved  # type: ignore[attr-defined]

    handler_by_name: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
        "memory.search": resolved.handlers.search,
        "memory.get_project": resolved.handlers.get_project,
        "memory.get_entity": resolved.handlers.get_entity,
        "memory.get_related": resolved.handlers.get_related,
        "memory.get_decisions": resolved.handlers.get_decisions,
        "memory.get_timeline": resolved.handlers.get_timeline,
        "memory.get_sources": resolved.handlers.get_sources,
        "memory.explain": resolved.handlers.explain,
        "memory.get_artifact": resolved.handlers.get_artifact,
        "memory.get_current_state": resolved.handlers.get_current_state,
        "memory.add_episode": resolved.handlers.add_episode,
        "memory.record_decision": resolved.handlers.record_decision,
    }
    missing = sorted(set(spec.names) - set(handler_by_name))
    if missing:  # pragma: no cover - a contract change without an implementation
        raise RuntimeError(f"tools.json declares tools with no handler: {missing}")

    for tool_spec in spec.tools:
        pipeline = _make_pipeline(resolved, tool_spec, handler_by_name[tool_spec.name])
        mcp._tool_manager._tools[tool_spec.name] = _build_tool(tool_spec, pipeline)

    _register_resources(mcp, resolved)
    _register_health(mcp, resolved)
    return mcp


def _make_pipeline(
    state: ServerState,
    tool_spec: ToolSpec,
    handler: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Wrap one handler in validation, the ADR-0008 gate, timing and audit."""
    schema = tool_spec.input_schema
    is_write = tool_spec.is_write

    async def pipeline(
        arguments: dict[str, Any], context: Context[Any, Any, Any] | None = None
    ) -> dict[str, Any]:
        from safeguards import redact_arguments

        timer = Stopwatch()
        state.calls_total += 1
        client_id = client_id_for(context)
        raw = dict(arguments or {})
        confirmed = raw.get("confirm") is True

        async def audited(
            *,
            allowed: bool,
            denied_reason: str | None = None,
            result_ref: str | None = None,
        ) -> None:
            if not (is_write or AUDIT_READS):
                return
            await state.audit.record(
                AuditRecord(
                    tool=tool_spec.name,
                    kind=tool_spec.kind,
                    client_id=client_id,
                    arguments=redact_arguments(raw),
                    confirmed=confirmed,
                    allowed=allowed,
                    denied_reason=denied_reason,
                    result_ref=result_ref,
                    latency_ms=timer.ms,
                )
            )

        async def refuse(reason: str, message: str) -> None:
            state.refusals_total += 1
            await audited(allowed=False, denied_reason=reason)
            logger.info(
                "mcp.refused",
                tool=tool_spec.name,
                client_id=client_id,
                reason=reason,
                latency_ms=timer.ms,
            )
            raise ToolError(message)

        # 1. The ADR-0008 gate runs before anything reads the payload, so "writes are off" is the
        #    first fact established about a write call - and so an oversized or unconfirmed body is
        #    audited under its own reason rather than as a generic schema error.
        if is_write:
            gateway = await state.gateway_status()
            denial = state.gate.evaluate(
                tool_spec.name,
                raw,
                client_id=client_id,
                gateway_writes_enabled=gateway.writes_enabled,
            )
            if denial is not None:
                await refuse(denial.reason, denial.message)

        # 2. The frozen contract, enforced (additionalProperties, lengths, enums, const).
        try:
            validated = validate_arguments(raw, schema, path=tool_spec.name)
        except SchemaViolation as exc:
            await refuse("invalid_arguments", str(exc))
            raise  # pragma: no cover - refuse always raises

        # 3. The handler.
        try:
            result = await handler(validated, client_id=client_id)
        except ApiError as exc:
            state.errors_total += 1
            await audited(allowed=False, denied_reason=exc.code)
            raise ToolError(exc.message) from None
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - nothing unexpected may leak to a client
            state.errors_total += 1
            logger.error("mcp.tool_failed", tool=tool_spec.name, error=type(exc).__name__)
            await audited(allowed=False, denied_reason="internal_error")
            raise ToolError("The memory server could not complete this call.") from None

        await audited(allowed=True, result_ref=_result_ref(result))
        logger.info(
            "mcp.call",
            tool=tool_spec.name,
            kind=tool_spec.kind,
            client_id=client_id,
            latency_ms=timer.ms,
        )
        return result

    pipeline.__name__ = tool_spec.name.replace(".", "_")
    return pipeline


def _result_ref(result: dict[str, Any]) -> str | None:
    """The created object's id for a write, so an audit row points at what it produced."""
    if not isinstance(result, dict):
        return None
    for key in ("artifact_id", "episode_id", "object_id"):
        value = result.get(key)
        if value:
            return str(value)
    return None


# =================================================================================== resources ===


def _register_resources(mcp: FastMCP, state: ServerState) -> None:
    """The two ``memory://`` resources from ``tools.json``."""
    by_uri = {r["uri"]: r for r in state.spec.resources}

    registry = by_uri.get("memory://projects", {})

    @mcp.resource(
        "memory://projects",
        name=registry.get("name", "Project registry"),
        description=registry.get("description", ""),
        mime_type=registry.get("mimeType", "application/json"),
    )
    async def projects_resource() -> str:
        return json.dumps(await state.handlers.resource_projects(), default=str)

    project_state = by_uri.get("memory://project/{id}/state", {})

    @mcp.resource(
        "memory://project/{id}/state",
        name=project_state.get("name", "Project state"),
        description=project_state.get("description", ""),
        mime_type=project_state.get("mimeType", "application/json"),
    )
    async def project_state_resource(id: str) -> str:  # noqa: A002 - the URI template names it "id"
        return json.dumps(await state.handlers.resource_project_state(id), default=str)


# ====================================================================================== health ===


def _register_health(mcp: FastMCP, state: ServerState) -> None:
    """``GET /health`` for the compose healthcheck and for a human checking the write posture."""

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> JSONResponse:
        gateway = await state.gateway_status()
        writes_enabled = state.mcp_write_enabled and gateway.writes_enabled
        body = {
            "status": "ok" if gateway.reachable else "degraded",
            "at": utc_now().isoformat(),
            "server": SERVER_NAME,
            "transport": "streamable-http",
            "mcp_path": "/mcp",
            "contract": {
                "version": state.spec.version,
                "tools": len(state.spec.tools),
                "read_tools": sum(1 for t in state.spec.tools if t.kind == "read"),
                "write_tools": len(state.spec.write_names),
                "resources": len(state.spec.resources),
            },
            "gateway": {
                "reachable": gateway.reachable,
                "status": gateway.status,
                "writes_enabled": gateway.writes_enabled,
            },
            "writes": {
                "effective": writes_enabled,
                "mcp_write_enabled": state.mcp_write_enabled,
                "gateway_write_enabled": gateway.writes_enabled,
                "confirm_required": state.spec.confirm_required,
                "rate_limit_per_minute": state.spec.rate_limit_per_minute,
                "max_text_chars": state.spec.max_text_chars,
            },
            "audit": state.audit.stats(),
            "counters": {
                "calls_total": state.calls_total,
                "refusals_total": state.refusals_total,
                "errors_total": state.errors_total,
            },
        }
        # 200 even when the Gateway is down: the container is alive and answering, and a restart
        # loop on mcp-server would not help a memory-api outage. The body says "degraded".
        return JSONResponse(body, status_code=200)
