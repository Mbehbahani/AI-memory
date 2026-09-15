# Operations

Runbooks written by A14 (and A10/A11 for their areas) during development:

- `runbook.md` — start/stop, first run, ingest tiers, status, reprocess, logs, health
- `add-a-source-root.md` — `.env` + compose mount + `config/source-roots.yaml` + `.memoryignore` overrides
- `backup-restore.md` — must-back-up vs recreatable; commands; restore drill
- `troubleshooting.md` — symptoms → fixes
- `scheduling.md` — Windows Task Scheduler snippet (documented, not installed)
- `mcp.md` — connecting an MCP client, enabling writes, what is audited (A10)
- `queries.md` / `visualization.md` — saved Cypher queries and NeoDash usage (A11)
- `bedrock-extraction.md` — enabling `LLM_PROVIDER=bedrock` in Docker: the optional read-only `~/.aws`
  mount, `AWS_PROFILE`/`BEDROCK_REGION` wiring, how to turn it off (A03, 2026-09-15 recovery fix)
