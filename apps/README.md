# Apps

Thin deployable services. All logic lives in `packages/aimemory`; each app has a `Dockerfile`
(python:3.12-slim, non-root) and a small entrypoint module.

| App | Entry | Port (internal) | Owner |
|---|---|---|---|
| `ingestion/` | `aimemory-ingest` CLI (`run`, `scan`, `status`, `reprocess`, `migrate`, `rebuild-graph`) | — | A07a (A04 for `migrate`) |
| `memory-api/` | FastAPI `aimemory.gateway` over REST `/v1/*`, `/health`, `/metrics` | 8000 | A09 |
| `mcp-server/` | FastMCP server over memory-api HTTP; streamable-HTTP `/mcp`, `/health` | 8020 | A10 |
| `embedding-service/` | FastAPI + sentence-transformers MiniLM; `/embed`, `/v1/embeddings`, `/health` | 8010 | A06 |

Dockerfiles are created by A03 in P2-T02.
