# ADR-0008 — MCP writes are off by default, confirmed, audited, append-only

Status: accepted · Date: 2026-09-13

## Decision
`memory.add_episode` and `memory.record_decision` exist in V0.1 but are disabled unless
`MCP_WRITE_ENABLED=true` (and `GATEWAY_WRITE_ENABLED=true`). Each call requires `confirm=true`, is
rate-limited (10/min), size-limited (8,000 chars), tagged with the client id, and logged in
`mcp_audit_log`. Writes only append (new episodes/artifacts; supersession links) — never update or
delete existing rows. Read tools never return raw file bytes. The mcp-server holds no database
credentials; it talks only to memory-api.

## Consequences
+ A future AI client cannot corrupt memory silently. − Two flags to flip when writes are wanted (documented).
