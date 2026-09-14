# Scripts

PowerShell (`.ps1`) and bash (`.sh`) pairs, created by A03 in P2-T02. Planned:

| Script | Purpose |
|---|---|
| `init-env` | Create `.env` from `.env.example` with generated passwords (never prints them) |
| `up` / `down` | `docker compose up -d` (with `--profile viz` optional) / `docker compose stop` (never `-v`) |
| `doctor` | Health of every service, port bindings (loopback only), disk, model presence; `--security` runs the A13 checklist |
| `ingest` | `docker compose run --rm ingestion aimemory-ingest run --root <root> [--tier 0|1|2]` |
| `backup` / `restore` | `pg_dump -Fc` to `backups/postgres/`, config copy; restore drill |
| `rebuild-graph` | Recreate Neo4j projection from PostgreSQL |
| `eval` | Run the retrieval gold set and write `reports/evaluation-<ts>.md` |
| `mcp-stdio` | stdio launcher proxying to the MCP HTTP server (for clients that need stdio) |
