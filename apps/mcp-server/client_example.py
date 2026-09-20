"""A real MCP client against this server - the example from ``docs/operations/mcp.md``. Owner: A10.

Uses the ``mcp`` SDK exactly as Claude Code or Claude Desktop would: open a streamable-HTTP session,
``initialize``, list the tools and resources, call a few read tools against the live corpus, and try
a write so the ADR-0008 refusal is visible. It prints what it got; it asserts nothing. The assertions
live in ``tests/integration/test_mcp_*.py``.

Run it inside the compose network (the tools container can reach ``mcp-server`` by service name)::

    docker compose --profile tools run --rm tools \
        python apps/mcp-server/client_example.py http://mcp-server:8020/mcp

or from the host, where the published port is loopback-only::

    python apps/mcp-server/client_example.py http://127.0.0.1:8020/mcp
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

DEFAULT_URL = "http://mcp-server:8020/mcp"

#: Identifies this client in ``mcp_audit_log.client_id`` and to the rate limiter.
CLIENT_INFO = Implementation(name="ai-memory-example", version="0.1.0")


def _short(payload: Any, limit: int = 600) -> str:
    text = json.dumps(payload, indent=2, default=str)
    return text if len(text) <= limit else text[:limit] + f"\n  ... ({len(text)} chars)"


def _content(result: Any) -> Any:
    """Unwrap the first text block of a CallToolResult into JSON when it is JSON."""
    for block in result.content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            return json.loads(text)
        except ValueError:
            return text
    return None


async def main(url: str) -> int:
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write, client_info=CLIENT_INFO) as session:
            init = await session.initialize()
            print(f"connected to {init.serverInfo.name} {init.serverInfo.version}")

            tools = (await session.list_tools()).tools
            print(f"\ntools ({len(tools)}):")
            for tool in tools:
                print(f"  {tool.name:<28} {tool.title}")

            resources = (await session.list_resources()).resources
            templates = (await session.list_resource_templates()).resourceTemplates
            print("\nresources:")
            for item in resources:
                print(f"  {item.uri}")
            for item in templates:
                print(f"  {item.uriTemplate}  (template)")

            print("\nmemory://projects:")
            registry = await session.read_resource(resources[0].uri)
            print(_short(json.loads(registry.contents[0].text), 300))

            print("\nmemory.search('retrieval pipeline', k=3):")
            hits = _content(
                await session.call_tool(
                    "memory.search", {"query": "retrieval pipeline", "k": 3, "expand": True}
                )
            )
            print(_short(hits, 900))

            first_project = json.loads(registry.contents[0].text)["projects"][0]["id"]
            print(f"\nmemory.get_project({first_project!r}):")
            print(_short(_content(await session.call_tool(
                "memory.get_project", {"id_or_name": first_project}
            )), 500))

            print("\nmemory.get_decisions(include_superseded=false):")
            decisions = _content(await session.call_tool("memory.get_decisions", {}))
            print(_short({"count": decisions["count"]}, 200))

            if decisions["decisions"]:
                artifact_id = decisions["decisions"][0]["id"]
                print(f"\nmemory.explain({artifact_id}):")
                print(_short(_content(
                    await session.call_tool("memory.explain", {"object_id": artifact_id})
                ), 700))

            print("\nmemory.add_episode (expected to be refused while writes are off):")
            refused = await session.call_tool(
                "memory.add_episode", {"text": "example write attempt", "confirm": True}
            )
            print(f"  isError={refused.isError}")
            print(f"  {_content(refused)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL)))
