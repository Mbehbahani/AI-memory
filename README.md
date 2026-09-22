# AI Memory — Personal AI Knowledge Infrastructure (V0.1, local Docker)

Persistent, provenance-aware, temporal memory for a future personal AI. Runs entirely on this
machine with Docker Compose: PostgreSQL + pgvector, Neo4j, Ollama (Qwen3 4B), a MiniLM embedding
service, an ingestion engine, a Memory Gateway API, an MCP server, and NeoDash.

```text
Sources (my-vault, project repos; read-only mounts)
   ↓ Episodes / Changes
   ↓ Knowledge Artifacts
   ↓ Temporal Knowledge Graph (Neo4j)  +  Semantic Vector Index (pgvector)
   ↓ Memory Gateway  →  MCP  →  Future AI clients
```

## Status

**Implemented V0.1 with documented gaps.** The repository has running services, PostgreSQL
migrations, incremental Tier 0/1 ingestion, a native Tier 2 extraction engine, hybrid retrieval,
provenance-aware Gateway endpoints, MCP tooling, security checks, and a clean-start verification.
The architecture target remains documented in
[docs/architecture/v0.1-plan.md](docs/architecture/v0.1-plan.md); the measured implementation
status is in [reports/final-build-report.md](reports/final-build-report.md).

The current limitations matter: the latest reconciliation reports an empty Neo4j projection,
Tier 2 is a 15/144 episode pilot rather than a complete background run, the JobLab DE deep pilot
is not complete, and several security/data-quality follow-ups remain open. Do not read the design
documents as proof that every planned capability is complete.

## Principles

- Centralize knowledge, not necessarily raw files. Original project files remain the source of truth.
- PostgreSQL is the system of record; Neo4j is a rebuildable projection.
- Every important memory retains provenance. Memory is temporal, never destructively overwritten.
- Ingestion is incremental; unchanged data is not reprocessed.
- Future AI clients access memory only through the Memory Gateway / MCP.
- Local models, local privacy, Docker Compose. Major technology changes never happen silently.

## Layout

| Path | Purpose |
|---|---|
| `apps/` | Deployable services (thin; logic lives in `packages/aimemory`) |
| `packages/aimemory/` | The single Python package: domain, ontology, sources, extractors, chunking, providers, knowledge, retrieval, gateway, provenance, persistence, common |
| `infra/` | Postgres init + Alembic migrations, Neo4j schema, Ollama Modelfile, NeoDash dashboard, shared Docker bits |
| `schemas/` | Machine-readable contracts: ontology, LLM extraction JSON Schemas, MCP tool schemas, OpenAPI |
| `config/` | Source roots, storage policies, retrieval weights (no absolute paths; env-driven) |
| `scripts/` | up/down/ingest/backup/restore/rebuild-graph/eval/doctor (PowerShell + bash) |
| `tests/` | unit, integration, memory scenarios, evaluation gold set, fixtures |
| `docs/` | architecture, ADRs, operations, security, development (agent matrix, task plan) |
| `reports/` | Build Observer output (agent runs, ledger, timeline, tests, final report) |
| `.claude/agents/` | Development agent definitions (workers only; not runtime components) |

## Quick start (local Docker Compose)

```powershell
copy .env.example .env      # then generate passwords with scripts/init-env.ps1
docker compose up -d
docker compose run --rm ingestion aimemory-ingest run --root vault
```

Endpoints (loopback only): memory-api `http://127.0.0.1:8000`, MCP `http://127.0.0.1:8020/mcp`,
Neo4j Browser `http://127.0.0.1:7474`, NeoDash `http://127.0.0.1:5005`.

## Documents

- [V0.1 plan](docs/architecture/v0.1-plan.md) · [ADRs](docs/adr/README.md)
- [Agent matrix](docs/development/agent-matrix.md) · [Task plan](docs/development/task-plan.md)
- [Reports](reports/README.md) · [CHANGELOG](CHANGELOG.md)
- [Manager review package](docs/manager-review-package.md) · [Repository tree](docs/repository-tree.txt)


- [Start message](docs/development/START-MESSAGE.md) — what to paste to begin development
