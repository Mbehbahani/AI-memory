"""The only outbound edge of the MCP server: HTTP to memory-api. Owner: A10.

ADR-0008 says the mcp-server holds **no database credentials**. This module is where that promise is
kept: it is the single place that talks to anything outside the process, it speaks only HTTP to
``MEMORY_API_URL``, and nothing in ``apps/mcp-server`` imports :mod:`aimemory.persistence` or opens a
driver. Every scoping, temporal, provenance and "URIs, never bytes" rule therefore stays where A09
enforces it - the MCP server cannot route around the Gateway because it has nothing to route with.

Errors are sanitized on the way out (plan section T). memory-api already returns
``{"error", "message", "context"}`` with a public message; anything else - a connect error, a
timeout, an unexpected status - collapses to a short, fixed sentence. A URL, a status line or a
stack trace never reaches an MCP client.
"""

from __future__ import annotations

from typing import Any

import httpx
from aimemory.common.config import get_settings
from aimemory.common.logging import get_logger

__all__ = ["ApiError", "MemoryApiClient"]

logger = get_logger(__name__)

#: Read calls that hit pgvector + Neo4j + the embedding service can legitimately take a few seconds.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=30.0, pool=5.0)


class ApiError(RuntimeError):
    """A memory-api call failed. ``message`` is already safe to show a client.

    ``status`` and ``code`` are kept so a caller can tell "not found" from "writes are disabled"
    without parsing prose - the audit record uses ``code`` as its ``denied_reason``.
    """

    def __init__(self, message: str, *, status: int | None = None, code: str = "api_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class MemoryApiClient:
    """A thin async wrapper over the memory-api paths that ``tools.json`` maps onto.

    One client per process (created in the FastMCP lifespan) so connections are pooled; every method
    returns plain JSON, already unwrapped, and raises :class:`ApiError` for anything else.
    """

    def __init__(self, base_url: str | None = None, *, client: httpx.AsyncClient | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.gateway.url).rstrip("/")
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=DEFAULT_TIMEOUT,
            headers={"user-agent": "ai-memory-mcp/0.1"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- transport ------------------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(method, path, params=params, json=json_body)
        except httpx.TimeoutException:
            logger.warning("mcp.api_timeout", method=method, path=path)
            raise ApiError(
                "The memory service did not answer in time.", code="api_timeout"
            ) from None
        except httpx.HTTPError as exc:
            # The exception text can contain the resolved URL; log it locally, never return it.
            logger.warning("mcp.api_unreachable", method=method, path=path, error=type(exc).__name__)
            raise ApiError("The memory service is unreachable.", code="api_unreachable") from None

        if response.status_code >= 400:
            raise self._to_error(response, method=method, path=path)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            logger.warning("mcp.api_bad_json", method=method, path=path)
            raise ApiError("The memory service returned an unreadable response.") from None

    @staticmethod
    def _to_error(response: httpx.Response, *, method: str, path: str) -> ApiError:
        code = "api_error"
        message = "The memory service could not complete the request."
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            # A09 already guarantees this field is sanitized (apps/memory-api/errors.py).
            message = payload["message"]
            code = str(payload.get("error") or code)
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                fields = [
                    ".".join(str(p) for p in item.get("loc", [])[1:])
                    for item in errors
                    if isinstance(item, dict)
                ]
                named = ", ".join(f for f in fields if f)
                if named:
                    message = f"{message} ({named})"
        logger.warning(
            "mcp.api_error", method=method, path=path, status=response.status_code, code=code
        )
        return ApiError(message, status=response.status_code, code=code)

    # ---- system ---------------------------------------------------------------------------------
    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    # ---- read -----------------------------------------------------------------------------------
    async def search(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/search", json_body=body)

    async def list_projects(self, project_ids: list[str] | None = None) -> list[dict[str, Any]]:
        params = [("project_id", p) for p in (project_ids or [])]
        return await self._request("GET", "/v1/projects", params=params or None)

    async def get_project(self, project_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/projects/{project_id}")

    async def get_entity(
        self, entity_id: str, *, as_of: str | None = None, include_related: bool = True
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_related": str(include_related).lower()}
        if as_of:
            params["as_of"] = as_of
        return await self._request("GET", f"/v1/entities/{entity_id}", params=params)

    async def get_related(
        self,
        entity_ids: list[str],
        *,
        as_of: str | None = None,
        project_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        params: list[tuple[str, Any]] = [("entity_id", e) for e in entity_ids]
        params += [("project_id", p) for p in (project_ids or [])]
        if as_of:
            params.append(("as_of", as_of))
        return await self._request("GET", "/v1/related", params=params)

    async def get_decisions(
        self,
        *,
        project_ids: list[str] | None = None,
        include_superseded: bool = False,
        as_of: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [("project_id", p) for p in (project_ids or [])]
        params.append(("include_superseded", str(include_superseded).lower()))
        params.append(("limit", limit))
        if as_of:
            params.append(("as_of", as_of))
        return await self._request("GET", "/v1/decisions", params=params)

    async def get_timeline(
        self,
        *,
        project_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        from_at: str | None = None,
        to_at: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [("project_id", p) for p in (project_ids or [])]
        params += [("entity_id", e) for e in (entity_ids or [])]
        if from_at:
            params.append(("from", from_at))
        if to_at:
            params.append(("to", to_at))
        params.append(("limit", limit))
        return await self._request("GET", "/v1/timeline", params=params)

    async def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/artifacts/{artifact_id}")

    async def get_state(
        self, *, project_ids: list[str] | None = None, as_of: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        params: list[tuple[str, Any]] = [("project_id", p) for p in (project_ids or [])]
        params.append(("limit", limit))
        if as_of:
            params.append(("as_of", as_of))
        return await self._request("GET", "/v1/state", params=params)

    async def explain(self, object_id: str, *, object_type: str | None = None) -> dict[str, Any]:
        params = {"object_type": object_type} if object_type else None
        return await self._request("GET", f"/v1/explain/{object_id}", params=params)

    # ---- write (ADR-0008; refused by memory-api unless GATEWAY_WRITE_ENABLED) ---------------------
    async def add_episode(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/episodes", json_body=body)

    async def record_decision(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/v1/decisions", json_body=body)

    # ---- audit (ADR-0008 mcp_audit_log; see audit.py for the fallback when it is absent) ----------
    async def post_audit(self, body: dict[str, Any]) -> dict[str, Any] | None:
        return await self._request("POST", "/v1/mcp/audit", json_body=body)
