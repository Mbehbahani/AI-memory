# Scripts

PowerShell (`.ps1`, primary) and bash (`.sh`) pairs. `init-env`/`up`/`down`/`doctor`/`ingest`/`backup`/
`restore`/`rebuild-graph`/`test` were created by A03 in P2-T02. Every script sets strict error handling
(`$ErrorActionPreference='Stop'` / `set -euo pipefail`), has a usage comment/`.SYNOPSIS`, and never
prints secrets.

| Script | Purpose |
|---|---|
| `init-env` | Create `.env` from `.env.example` with cryptographically random `POSTGRES_PASSWORD`/`NEO4J_PASSWORD`/`NEO4J_READONLY_PASSWORD` (never printed) |
| `up` | `docker compose up -d` (base + dev override auto-loaded); `-Viz`/`--viz` also starts NeoDash |
| `down` | `docker compose down --remove-orphans` -- refuses to run if `-v`/`--volumes` is requested; named volumes are always preserved |
| `doctor` | Docker daemon, `docker compose config`, per-service health, published-port loopback check (`docker inspect` + `netstat`), `qwen3:4b` presence, disk space; `-Security`/`--security` runs a stub section for A13 (P15-T01) |
| `ingest` | `docker compose exec ingestion aimemory-ingest run --root <root> [--tier 0\|1\|2]` (falls back to `run --rm` if the worker isn't up) |
| `rebuild-graph` | Recreate the Neo4j projection from PostgreSQL via `aimemory-ingest rebuild-graph` |
| `backup` | `pg_dump -Fc` (run inside the postgres container, using its own env vars) to `backups/postgres/<timestamp>.dump`; copies `.env`, `config/`, `.memoryignore`, and the NeoDash dashboard (if present) to `backups/config/<timestamp>/` |
| `restore` | `pg_restore --clean --if-exists` from a `backups/postgres/*.dump` file or bare timestamp; does not touch Neo4j (rebuild it afterwards) or config files (copy back by hand) |
| `test` | Runs pytest inside the `tools` compose profile (Python 3.12, matches the runtime containers -- the host runs 3.14); `-Unit`/`--unit` skips `integration`/`memory`/`evaluation`/`slow`-marked tests |

Not yet created (owned by later phases, per `docs/development/task-plan.md`):

| Script | Purpose | Owner / phase |
|---|---|---|
| `eval` | Run the retrieval gold set and write `reports/evaluation-<ts>.md` | A12, P14-T05 |
| `mcp-stdio` | stdio launcher proxying to the MCP HTTP server | A10, P11-T01 |
