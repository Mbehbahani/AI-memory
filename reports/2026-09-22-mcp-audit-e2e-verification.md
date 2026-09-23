# MCP audit end-to-end verification — 2026-09-22

**Result: PASS (live-write control verified).** `MCP_WRITE_ENABLED=true` and
`GATEWAY_WRITE_ENABLED=true` were already effective in the running MCP health response. This was
therefore a production-path audit check, not a pre-enablement check. No implementation was changed.

## Scope and safety

Two real MCP `memory.add_episode` calls were deliberately refused by sending `confirm=false`.
This exercised the historically missing audit case while adding no memory content. The first call
was made before, and the second after, a local restart of `ai-memory-memory-api-1`.

## Exact commands run

```powershell
docker ps

@'
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation
async def main():
    async with streamable_http_client("http://mcp-server:8020/mcp") as (read, write, _):
        async with ClientSession(read, write, client_info=Implementation(name="mcp-audit-e2e", version="2026-09-22")) as session:
            await session.initialize()
            result = await session.call_tool("memory.add_episode", {"text": "MCP audit E2E refused-write verification", "confirm": False})
            print(result.isError, result.content[0].text)
asyncio.run(main())
'@ | docker compose --profile tools run --rm -T tools python -

docker restart ai-memory-memory-api-1

# Waited until GET http://127.0.0.1:8010/health returned status=ok.

# Repeated the MCP command above with text:
# "MCP audit E2E refused-write verification after memory-api restart"

@'
SELECT id, at, tool, kind, client_id, confirmed, allowed, denied_reason, result_ref, latency_ms
FROM mcp_audit_log
WHERE client_id = 'mcp-audit-e2e/2026-09-22'
ORDER BY at;
'@ | docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off'

Invoke-RestMethod http://127.0.0.1:8020/health
```

The stack was healthy before the test. After the restart, `memory-api` returned `status=ok` with
its embedding model loaded before the second call.

## Direct PostgreSQL evidence

| Order | Audit row ID | UTC time | Result |
| --- | --- | --- | --- |
| Before restart | `92fd764d-2bb4-4a40-b8f5-aefacedaa697` | 2026-09-22 16:04:54.212116+00 | `memory.add_episode`; write; `confirmed=false`; `allowed=false`; `denied_reason=confirm_required`; latency 1 ms |
| After restart | `0f3eddf3-8e21-45d4-ab7b-aa99b5c6f048` | 2026-09-22 16:05:25.607277+00 | `memory.add_episode`; write; `confirmed=false`; `allowed=false`; `denied_reason=confirm_required`; latency 0 ms |

Both rows are in `mcp_audit_log`, queried directly from PostgreSQL.

## MCP audit-sink health

After the second write, the MCP server's own `/health` response reported:

```text
audit.sink       = gateway
audit.degraded   = false
audit.submitted  = 4
audit.persisted  = 4
audit.dropped    = 0
```

Thus the restart did not leave audit persistence latched to `local`; the new refused write reached
the gateway and Postgres. The audit path is functioning for the live-write configuration. This
check validates recovery for a post-restart write; it does not claim to have submitted a write
during the brief outage itself.
