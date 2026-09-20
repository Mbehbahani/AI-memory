# Connecting an AI client to AI Memory (MCP)

The MCP server exposes this memory to an AI client (Claude Code, Claude Desktop, or anything else
that speaks MCP). It runs in the `mcp-server` container and listens on **`127.0.0.1:8020/mcp`** —
loopback only, like every other service in this stack.

It holds **no database credentials**. Every tool call goes over HTTP to `memory-api`, which owns the
only connection to PostgreSQL (ADR-0008). That boundary is the reason a misbehaving AI client cannot
reach your data directly.

## Start it

```bash
docker compose up -d
docker compose ps        # mcp-server should read "Up (healthy)"
```

## Connect a client

**HTTP (preferred, when the client supports it):**

```bash
claude mcp add --transport http ai-memory http://127.0.0.1:8020/mcp
```

**stdio (for clients that can only spawn a subprocess):**

```bash
scripts/mcp-stdio.sh          # or scripts\mcp-stdio.ps1 on Windows
```

The stdio launcher is a *pump*, not a second server: it forwards bytes to the running container over
HTTP. A stdio client and an HTTP client therefore hit the same write gate, the same rate limiter and
the same audit trail — there is no second set of safeguards to keep in sync.

Check it end to end with a real client:

```bash
docker compose --profile tools run --rm tools \
    python apps/mcp-server/client_example.py http://mcp-server:8020/mcp
```

## What the client gets

**10 read tools.** `memory.search` (hybrid vector + keyword + graph), `memory.get_project`,
`memory.get_entity`, `memory.get_related`, `memory.get_decisions`, `memory.get_timeline`,
`memory.get_sources`, `memory.explain`, `memory.get_artifact`, `memory.get_current_state`.

**2 write tools**, off by default — see below: `memory.add_episode`, `memory.record_decision`.

**2 resources.** `memory://projects` and `memory://project/{id}/state`.

The tool list is generated from `schemas/mcp/tools.json`, which is the frozen contract; a test
asserts the served list matches it exactly, so the two cannot drift.

`memory.explain` is the one worth knowing about. It answers *"where did this come from?"* with the
whole chain:

```
artifact <title> <- episode <file> <- version sha256:… <- source vault://… <- root my-vault
  <- device <machine> <- model <extraction model> <- run <ingestion run>
```

Read tools never return raw file bytes.

## Writes are off by default

Both flags must be true, on the containers named:

| Flag | Container | Default |
|---|---|---|
| `MCP_WRITE_ENABLED` | `mcp-server` | `false` |
| `GATEWAY_WRITE_ENABLED` | `memory-api` | `false` |

With either unset, a write tool returns an error explaining exactly which flag to set. Even when
enabled, every write (ADR-0008):

- requires `confirm=true` in the arguments,
- is rate-limited to **10 per minute**,
- is size-limited to **8,000 characters**,
- is tagged with the calling client's id,
- and **only ever appends** — new episodes and artifacts, plus supersession links. Nothing is ever
  updated or deleted, so a client cannot quietly rewrite what you already know.

To enable writes, set both variables in `.env`, then `docker compose up -d memory-api mcp-server`.
Turning them back off is the same edit in reverse.

## The audit trail

Every write attempt — **allowed or refused** — produces a row in `mcp_audit_log`: tool, kind, client
id, the arguments, whether it was confirmed, whether it was allowed, the refusal reason, and latency.

```bash
docker compose exec -T postgres psql -U aimemory -d aimemory \
  -c "SELECT at, tool, client_id, allowed, denied_reason FROM mcp_audit_log ORDER BY at DESC LIMIT 20;"
```

The MCP server posts these to `POST /v1/mcp/audit` on memory-api, since it has no database
credentials of its own. That route is deliberately **not** behind `GATEWAY_WRITE_ENABLED`: refusals
only happen while writes are disabled, so gating the audit sink on the write flag would drop exactly
the records the policy exists to keep.

If memory-api is unreachable, the server does **not** fail the tool call and does **not** pretend the
row was stored: it writes the record to its structured log with `persisted: false`, keeps a bounded
in-memory ring buffer, and reports itself degraded. Look for `mcp.audit_sink_missing` in the logs.

## Troubleshooting

**`mcp-server` never leaves `Created`.** It waits for `memory-api` to be healthy. Check
`docker compose ps` and then `docker compose logs memory-api`.

**Client connects but lists no tools.** Confirm you pointed it at the `/mcp` path, not the bare port.

**Writes refused although you set the flags.** Both are required, and they live on *different*
containers. Verify with:

```bash
docker compose exec mcp-server printenv MCP_WRITE_ENABLED
docker compose exec memory-api printenv GATEWAY_WRITE_ENABLED
```

**Audit rows are missing.** The sink caches its "missing route" state per process; after changing
memory-api, restart the MCP server with `docker compose restart mcp-server`.

**Search returns hits but `related_entities` is always empty.** Known limitation, not a
misconfiguration: `entity_mentions.chunk_id` is not yet populated, so a search hit cannot seed the
graph walk. Graph expansion itself is verified working against the populated graph.
