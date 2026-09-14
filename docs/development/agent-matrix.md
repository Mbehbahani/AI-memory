# Development Agent Matrix

Agents are build workers defined in `.claude/agents/`. They are not runtime components. The orchestrator
(A00, this Claude Code session, Opus) dispatches tasks from [task-plan.md](task-plan.md), enforces
ownership, and forwards every result to A01. Protocol: [agent-protocol.md](agent-protocol.md).

| agent_id | name (`.claude/agents/`) | model | role | responsibilities | inputs | outputs | owned files | dependencies | parallel with | acceptance | handoff → |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A00 | orchestrator (this session) | Opus | lead | sequencing, gates, blockers, commits, reviews | plan | dispatches | — | — | — | all phases done | — |
| A01 | `build-observer` | Sonnet | reporter | ledger, timeline, test summary, final report, CHANGELOG, ADR index | all results | `reports/*`, `CHANGELOG.md` | `reports/`, `CHANGELOG.md`, ADR index | — | everyone | artifacts complete, nothing fabricated | A00 |
| A02 | `architect-contracts` | Opus | architect | ADRs, `schemas/`, ontology, domain models, ports, config, architecture docs | plan | contracts | `docs/architecture/`, `docs/adr/`, `schemas/`, `domain/`, `ontology/`, `common/` | P0 | A03 | contracts importable + tested | A04–A10 |
| A03 | `infra-docker` | Sonnet | devops | compose, Dockerfiles, pins, healthchecks, scripts, backup | §E | running stack | compose files, `infra/docker`, `infra/ollama`, `infra/postgres/init`, `scripts/`, Dockerfiles | P2 | A02 | `docker compose up -d` healthy | A05, A06 |
| A04 | `data-model-migrations` | Sonnet (Opus review) | data | Alembic, SQLAlchemy, repositories, Neo4j constraints, read-only user, `migrate` | §G/H | migrations | `infra/postgres/alembic`, `infra/neo4j/schema`, `persistence/` | A02, A03 | A06, A07b | up/down clean; constraint tests | A07, A08 |
| A05 | `local-ai-validation` | Opus | ML integration | OllamaProvider, measurements, Graphiti gate, ADR-0009 | §M/N/O | reports + verdict | `providers/llm`, `tests/evaluation/local_ai`, gate reports | A03 | A04, A06 | labelled measurements; verdict | A08 |
| A06 | `embedding-service` | Sonnet | service | FastAPI embedder, OpenAI-compatible route, client adapter | §N | service | `apps/embedding-service`, `providers/embedding` | A03 | A04, A05 | 384-d, offline, health | A07a, A09 |
| A07a | `ingestion-core` | Opus | pipeline | state machine, change detection, restartability, tiering, CLI, vault seed, pilot ingest | §K/L | engine | `apps/ingestion`, `sources/` (core), `cli/`, `ingest_repo` | A04, A06 | A07b | memory scenarios pass | A08, A09 |
| A07b | `extractors-chunkers` | Sonnet | pipeline | extractors, chunkers, ignore rules, policies, secret detector, fixtures | §K | modules | `extractors/`, `chunking/`, policies/ignore/secrets, fixtures | A02 | A07a, A04, A05 | unit + adversarial tests | A07a |
| A08 | `temporal-graph-engine` | Opus | graph | KnowledgeEngine per verdict, entity resolution, temporal rules, projection, rebuild, provenance | §H/I/J/O | engine | `knowledge/`, `providers/graph`, `provenance/` | A04, A05, A07a | A09 | supersession/provenance tests | A09, A11 |
| A09 | `retrieval-gateway` | Opus | retrieval | hybrid retrieval, Gateway, memory-api | §P/Q | API | `retrieval/`, `gateway/`, `apps/memory-api` | A04, A06, A08 contracts | A08 | eval runs; health | A10, A12 |
| A10 | `mcp-server` | Opus | interface | MCP tools, safeguards, audit, stdio launcher | §R | MCP | `apps/mcp-server`, `schemas/mcp` | A09 | A11, A12 | SDK client tests | A12, A13 |
| A11 | `visualization` | Sonnet | viz | NeoDash dashboard, saved queries | §S | dashboards | `infra/neodash`, queries doc | A08 | A10, A12 | pages render on pilot data | A01 |
| A12 | `tests-evaluation` | Sonnet | QA | test structure, scenarios, gold set, eval runner | §Y | tests, eval report | `tests/` | contracts; services as they land | continuous | all scenarios covered | owners, A13 |
| A13 | `security-review` | Opus | security | threat model, checklist verification, findings | §T | `docs/security` | `docs/security`, doctor security section | all | — | checklist pass or listed | A14, A15 |
| A14 | `documentation` | Sonnet | docs | README, operations runbooks | all | docs | `README.md`, `docs/operations` | all | A13 | fresh-machine walkthrough | A01 |
| A15 | `final-integration-review` | Opus | reviewer | drift, dead code, e2e verification, clean deploy | all | review log | review-only edits (logged) | all | — | tests green; health green | A01 |
| A16 | `ops-dashboard` | Sonnet | operator UI | offline `/ops` page: health, runs + action buttons, coverage, quality trends, review queue, attention list | ADR-0010/0011 | page + tests | `packages/aimemory/ops`, `apps/memory-api/{templates,static,routes/ops.py}` | A09 (router), A04 (tables), A07a (worker) | A10, A11 | every button works; offline; tests | A12, A13, A14 |

## Conflict prevention
- One owner per path at a time; ownership changes only via a recorded handoff in the ledger.
- `schemas/` and `packages/aimemory/domain` freeze after P1 (A02 only, with ADR).
- Sonnet agents never edit `knowledge/`, `retrieval/`, `gateway/`.
- Parallel lanes only touch disjoint paths (see task plan lanes A/B/C).
- A15 review edits are logged with file + reason and the owner is informed.

## Model assignment rationale
Opus: contracts, local-AI validation and the Graphiti gate, ingestion state machine, temporal graph,
retrieval, MCP, security, final review — the places where a wrong abstraction is expensive.
Sonnet: Docker, migrations, embedding service, extractors/chunkers, dashboards, tests, docs, reporting —
well-specified work with clear acceptance criteria. Assignments may change with justification recorded
in the ledger.
