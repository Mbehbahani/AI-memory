"""The mcp-server as an MCP client sees it, against the live compose stack. Owner: A10 (P11-T01).

Everything here goes through the ``mcp`` SDK over streamable HTTP - the same code path Claude Code
uses - and against the real corpus, not a fixture. Skips (never fails) when the stack is not running:
``mcp_available`` handles that, as it does for every other integration module.

Deliberately *read-only*. Writes are refused under the default flags and the refusal is asserted; no
test here turns a flag on, because a test that appends to the corpus to prove writes work is a test
that changes the thing under measurement. The flags-on path is exercised against a throwaway
container in the P11-T01 result and belongs in P11-T02 (A12) if it is to be automated.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from aimemory.common.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_JSON = REPO_ROOT / "schemas" / "mcp" / "tools.json"

pytest.importorskip("mcp", reason="the mcp SDK is only installed in the api/mcp/tools images")

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

pytestmark = pytest.mark.integration

#: Service name on the compose network; the tests run inside the `tools` container.
MCP_HOST = "mcp-server"
CLIENT_INFO = Implementation(name="aimemory-pytest", version="0.1.0")


def _base_url() -> str:
    return f"http://{MCP_HOST}:{get_settings().mcp.host_port}"


def _run(coro_factory: Callable[[ClientSession], Any]) -> Any:
    """Open one MCP session, hand it to ``coro_factory``, return its result."""

    async def runner() -> Any:
        async with streamable_http_client(f"{_base_url()}/mcp") as (read, write, _):
            async with ClientSession(read, write, client_info=CLIENT_INFO) as session:
                await session.initialize()
                return await coro_factory(session)

    return asyncio.run(runner())


def _payload(result: Any) -> Any:
    """The first text block of a CallToolResult, parsed as JSON when it is JSON."""
    for block in result.content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            return json.loads(text)
        except ValueError:
            return text
    return None


@pytest.fixture(scope="module")
def mcp_url(mcp_available: bool) -> str:
    return _base_url()


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(TOOLS_JSON.read_text(encoding="utf-8"))


# ====================================================================================================
# Transport and posture
# ====================================================================================================


def test_health_reports_the_contract_and_the_write_posture(mcp_url: str) -> None:
    body = httpx.get(f"{mcp_url}/health", timeout=10.0).json()
    assert body["status"] in {"ok", "degraded"}
    assert body["mcp_path"] == "/mcp"
    assert body["contract"] == {
        "version": "0.1.0",
        "tools": 12,
        "read_tools": 10,
        "write_tools": 2,
        "resources": 2,
    }
    assert body["writes"]["confirm_required"] is True
    assert body["writes"]["rate_limit_per_minute"] == 10
    assert body["writes"]["max_text_chars"] == 8000


def test_a_real_client_sees_exactly_the_contract(mcp_url: str, contract: dict) -> None:
    tools = _run(lambda s: s.list_tools())
    served = {t.name: t for t in tools.tools}
    assert sorted(served) == sorted(t["name"] for t in contract["tools"])
    for expected in contract["tools"]:
        tool = served[expected["name"]]
        assert tool.title == expected["title"]
        assert tool.description == expected["description"]
        assert tool.inputSchema == expected["inputSchema"]


def test_both_resources_are_advertised_and_readable(mcp_url: str) -> None:
    async def probe(session: ClientSession) -> tuple[dict, dict]:
        resources = await session.list_resources()
        templates = await session.list_resource_templates()
        uris = {str(r.uri) for r in resources.resources}
        template_uris = {t.uriTemplate for t in templates.resourceTemplates}
        assert uris == {"memory://projects"}
        assert template_uris == {"memory://project/{id}/state"}
        registry = await session.read_resource("memory://projects")  # type: ignore[arg-type]
        payload = json.loads(registry.contents[0].text)
        first = payload["projects"][0]["id"]
        state = await session.read_resource(f"memory://project/{first}/state")  # type: ignore[arg-type]
        return payload, json.loads(state.contents[0].text)

    registry, state = _run(probe)
    assert registry["count"] == len(registry["projects"]) > 0
    assert state["project_id"]


# ====================================================================================================
# Read tools against the real corpus
# ====================================================================================================


def test_search_returns_cited_hits_and_an_assembled_context(mcp_url: str) -> None:
    result = _payload(
        _run(lambda s: s.call_tool("memory.search", {"query": "data pipeline", "k": 5}))
    )
    assert len(result["hits"]) > 0
    for hit in result["hits"]:
        assert hit["object_id"] and hit["object_type"]
        assert hit["provenance"]["source_uri"], "every hit must carry a logical source URI"
    assert result["context"]["blocks"], "assemble_context=true must produce a context block"
    # ADR-0008: read tools return indexed text and URIs, never bytes read from disk at call time.
    assert "file_bytes" not in result and "content" not in result


def test_every_read_tool_answers(mcp_url: str) -> None:
    """One call per read tool, with arguments derived from what is actually in the corpus."""

    async def probe(session: ClientSession) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        registry = json.loads(
            (await session.read_resource("memory://projects")).contents[0].text  # type: ignore[arg-type]
        )
        project_id = registry["projects"][0]["id"]

        seen["memory.search"] = _payload(
            await session.call_tool("memory.search", {"query": "retrieval", "k": 3})
        )
        seen["memory.get_project"] = _payload(
            await session.call_tool("memory.get_project", {"id_or_name": project_id})
        )
        seen["memory.get_current_state"] = _payload(
            await session.call_tool("memory.get_current_state", {"project_id": project_id})
        )
        seen["memory.get_decisions"] = _payload(await session.call_tool("memory.get_decisions", {}))
        seen["memory.get_timeline"] = _payload(await session.call_tool("memory.get_timeline", {}))

        decisions = seen["memory.get_decisions"]["decisions"]
        if decisions:
            artifact_id = decisions[0]["id"]
            seen["memory.get_artifact"] = _payload(
                await session.call_tool("memory.get_artifact", {"artifact_id": artifact_id})
            )
            seen["memory.get_sources"] = _payload(
                await session.call_tool("memory.get_sources", {"object_id": artifact_id})
            )
            seen["memory.explain"] = _payload(
                await session.call_tool("memory.explain", {"object_id": artifact_id})
            )

        entity_id = _first_entity_id(seen)
        if entity_id:
            seen["memory.get_entity"] = _payload(
                await session.call_tool("memory.get_entity", {"id_or_name": entity_id})
            )
            seen["memory.get_related"] = _payload(
                await session.call_tool("memory.get_related", {"entity": entity_id})
            )
        return seen

    seen = _run(probe)
    assert seen["memory.get_project"]["project"]["id"]
    assert seen["memory.get_current_state"]["project_id"]
    assert isinstance(seen["memory.get_decisions"]["count"], int)
    assert isinstance(seen["memory.get_timeline"]["count"], int)
    if "memory.explain" in seen:
        assert seen["memory.explain"]["explanation"]
        assert seen["memory.get_sources"]["steps"]
        # tools.json: get_sources is the chain, explain adds the sentence.
        assert "explanation" not in seen["memory.get_sources"]


def _first_entity_id(seen: dict[str, Any]) -> str | None:
    """An entity UUID discovered from what the read tools already returned, or None."""
    for fact in seen.get("memory.get_current_state", {}).get("current_facts", []):
        if fact.get("subject_entity_id"):
            return str(fact["subject_entity_id"])
    for entity in seen.get("memory.search", {}).get("related_entities", []):
        if entity.get("entity_id"):
            return str(entity["entity_id"])
    return None


def test_explain_refuses_a_non_uuid_without_leaking_anything(mcp_url: str) -> None:
    result = _run(lambda s: s.call_tool("memory.explain", {"object_id": "not-a-uuid"}))
    assert result.isError
    message = str(_payload(result))
    assert "UUID" in message
    for leak in ("Traceback", "postgresql", "psycopg", "http://", "/app/"):
        assert leak not in message


def test_unknown_arguments_are_refused_by_the_frozen_schema(mcp_url: str) -> None:
    result = _run(lambda s: s.call_tool("memory.search", {"query": "x", "top_k": 3}))
    assert result.isError
    assert "unknown argument" in str(_payload(result))


# ====================================================================================================
# ADR-0008 write posture
# ====================================================================================================


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("memory.add_episode", {"text": "a test episode", "confirm": True}),
        (
            "memory.record_decision",
            {
                "title": "A test decision",
                "statement": "This must never be written.",
                "project_id": "joblab",
                "confirm": True,
            },
        ),
    ],
)
def test_writes_are_refused_under_the_default_flags(
    mcp_url: str, tool: str, arguments: dict
) -> None:
    posture = httpx.get(f"{mcp_url}/health", timeout=10.0).json()["writes"]
    if posture["effective"]:
        pytest.skip("writes are enabled on this stack; the default-posture assertion does not apply")
    result = _run(lambda s: s.call_tool(tool, arguments))
    assert result.isError
    message = str(_payload(result))
    assert "disabled" in message.lower()
    assert "MCP_WRITE_ENABLED" in message or "GATEWAY_WRITE_ENABLED" in message


def test_a_refused_write_is_counted_and_never_reaches_the_gateway(mcp_url: str) -> None:
    before = httpx.get(f"{mcp_url}/health", timeout=10.0).json()
    if before["writes"]["effective"]:
        pytest.skip("writes are enabled on this stack")
    _run(
        lambda s: s.call_tool(
            "memory.add_episode", {"text": "refusal accounting probe", "confirm": True}
        )
    )
    after = httpx.get(f"{mcp_url}/health", timeout=10.0).json()
    assert after["counters"]["refusals_total"] > before["counters"]["refusals_total"]
    assert after["audit"]["submitted"] > before["audit"]["submitted"]
