# AI Memory V0.1 - Manager Review Package

**Prepared:** 2026-09-21
**Purpose:** Share the architecture, repository shape, Compose topology, and an honest implementation-status summary for external review.

## Start Here

1. [README](../README.md) - repository orientation and local quick start.
2. [Architecture plan](architecture/v0.1-plan.md) - approved target architecture and contracts.
3. [Final build report](../reports/final-build-report.md) - what was built, measured results, and open problems.
4. [Repository tree](repository-tree.txt) - curated source tree for review.
5. [Docker Compose](../docker-compose.yml) - base service topology. The local development override is [docker-compose.override.yml](../docker-compose.override.yml).

Do not share `.env`, `backups/`, Docker volumes, `.relay/`, caches, or generated runtime data. Use `.env.example` as the configuration template.

## Architecture In One View

```text
Read-only source roots
        |
        v
Ingestion worker
  discover -> policy -> hash/diff -> extract -> chunk -> embed
        |                                      |
        |                                      +--> PostgreSQL + pgvector
        |                                             sources, versions, chunks,
        |                                             embeddings, episodes, knowledge,
        |                                             provenance and operations
        +--> native temporal knowledge engine
                    |
                    +--> Neo4j rebuildable graph projection

PostgreSQL + graph projection
        |
        v
Memory Gateway / memory-api
  hybrid retrieval, temporal filters, provenance, context assembly
        |
        v
MCP server
        |
        v
Future AI clients

Optional local surfaces: Neo4j Browser, NeoDash, offline Ops dashboard
```

## Compose Services

| Service | Role | Exposure / persistence |
|---|---|---|
| `postgres` | Canonical system of record with pgvector, pg_trgm and pgcrypto | `pg_data`; loopback only in the development override |
| `neo4j` | Rebuildable graph projection and traversal store | `neo4j_data`, logs/import; loopback only |
| `ollama` / `ollama-init` | Optional local `qwen3:4b` extraction path | `local-llm` profile; `ollama_models`; loopback only |
| `embedding-service` | Offline MiniLM-L6-v2 HTTP embeddings | baked model, 384 dimensions; loopback only |
| `migrate` | One-shot Alembic and Neo4j schema setup | exits successfully after migration |
| `ingestion` | Always-on worker plus CLI for source scanning and extraction | source roots mounted read-only; state volume |
| `memory-api` | Memory Gateway REST API and `/ops` surface | loopback `:8000`; no source mounts |
| `mcp-server` | MCP transport and guarded client interface | loopback `:8020`; no database credentials |
| `neodash` | Optional graph visualization | `viz` profile, loopback `:5005` |
| `tools` | Optional test/evaluation runner | `tools` profile only |

All published ports are intended to bind to `127.0.0.1`. The base Compose file exposes only the Gateway, MCP and optional visualization surfaces; the development override exposes databases and model services for local inspection.

## Implementation Status

| Review area | Status at the latest repository evidence |
|---|---|
| Sources -> ingestion -> episodes/artifacts -> Gateway | Substantially implemented. Incremental scan, state machine, chunking, embeddings, episodes, native extraction and REST Gateway exist. |
| PostgreSQL / pgvector | Implemented as the source of truth with Alembic migrations, HNSW vectors, repositories and provenance view. |
| Neo4j graph | Projection code and contracts exist, but the latest reconciliation reports Neo4j at 0 nodes and `rebuild-graph` still failing because the structural rebuild driver is missing. Treat graph traversal as designed and partially implemented, not fully operational on the current corpus. |
| Local AI | MiniLM is built, offline and measured. Ollama/Qwen3 4B is built and measured. The chosen default is currently Bedrock Haiku 4.5 because it measured 61.2% relationship recall versus 40.2% for Qwen3; Ollama remains the private/offline option. Relay is also documented as a third route. |
| Dynamic memory | Hashing, deduplication, moved/deleted/modified detection, resumable jobs, valid/observed time, unconfirmed facts and supersession rules are implemented and tested. |
| Knowledge model | Ontology and persistence cover projects, subprojects, people, organizations, technologies, concepts, documents, repositories, decisions, requirements, tasks, experiments, datasets, findings, applications and infrastructure components. |
| Provenance | Strong for facts and retrieval hits: source URI, hash, version, root/device, model, episode and run are retained. The final report notes that artifacts still lack a useful evidence sentence in the current corpus. |
| Retrieval | Vector + keyword + RRF, metadata/project filters, temporal `as_of`/`since`, ranking boosts, provenance and context assembly are implemented. Graph expansion is coded but cannot contribute while Neo4j is empty. |
| MCP / isolation | The MCP server is implemented with 10 read tools and 2 write tools, Gateway-only access, confirmation, dual write flags, limits and audit intent. Do not describe writes as production-ready: the latest security evidence records an audit-sink recovery defect and recommends re-checking before enabling writes. |
| File policies | `IGNORE`, `CATALOG_ONLY`, `INDEX_CONTENT` and `MIRROR` are implemented with deny rules, `.memoryignore`, policy config and secret detection. |
| Security / Docker | Read-only source mounts, loopback ports, health checks, persistent volumes, no Gateway source mounts, secret exclusion and clean-start verification are evidenced. Neo4j Community cannot enforce a true read-only database user; this is documented as an accepted limitation. Bedrock egress and an administrator-scoped AWS profile are open security concerns. |
| Visualization | Neo4j Browser, NeoDash configuration and an offline Ops dashboard are present. Current graph population is the blocking operational gap. |
| Maintainability | Provider, embedding, engine, ontology, persistence, retrieval, Gateway and MCP contracts are modular. Model/provider changes are recorded through ADRs and provenance. |
| End-to-end completeness | Not complete. Tier 2 is a deliberate pilot rather than a full background run; JobLab DE deep validation, graph rebuild, formal gold-set evaluation and final security remediation remain open. |

## Measured Evidence

- Full test run recorded in the final reports: **810 passing, 3 skipped**.
- Clean deployment verification: fresh isolated Compose project reached healthy Postgres, Neo4j, embedding-service, memory-api, mcp-server and ingestion; MCP listed 12 tools.
- Search evaluation: **67% hit@5** after the latest reported fix, with **100% provenance completeness** on returned answers.
- Tier 1 vault indexing: **241 sources and 4,757 chunks** in the final build report; earlier reconciliation snapshots differ because the corpus changed during development. Treat report dates as part of every number.
- Extraction comparison: Haiku **7.74 seconds median per episode / 61.2% relationship recall**; Qwen3 4B **174.43 seconds / 40.2%** on the same benchmark campaign.

## Recommended Review Focus

1. Verify the gap between the architecture contract and the latest implementation status, especially graph rebuild and full Tier 2 coverage.
2. Distinguish repository-level clean-start proof from full data-quality proof.
3. Review the 92 known incorrect or misdirected facts before enabling writes or relying on current project-technology answers.
4. Review the high-severity AWS credential scope finding and the open MCP audit-sink issue.
5. Confirm whether the simpler native engine is preferable to reviving Graphiti, given the measured local latency and the current projection gap.

## Canonical Evidence Files

- [Architecture plan](architecture/v0.1-plan.md)
- [Data model](architecture/data-model.md)
- [Ontology and projection](architecture/ontology.md)
- [Temporal model](architecture/temporal.md)
- [Retrieval specification](architecture/retrieval.md)
- [ADR index](adr/README.md)
- [Task plan](development/task-plan.md)
- [Final build report](../reports/final-build-report.md)
- [Known limitations](../reports/known-limitations.md)
- [Security threat model](security/threat-model.md)
- [Security checklist](security/checklist-2026-09-17.md)
- [Clean deployment verification](../reports/deploy-verification.md)
- [Test summary](../reports/test-summary.md)
