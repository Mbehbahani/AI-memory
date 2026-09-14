# Development Task Plan — V0.1

Phases are internal execution phases. After **START DEVELOPMENT** the orchestrator runs P0 → P17 without
stopping unless: (1) a destructive action outside the project boundary is required, (2) required access
is missing, (3) a major approved assumption proves invalid, (4) a major technology change is necessary.

Status legend: `todo` · `in-progress` · `done` · `blocked` · `skipped(reason)`. A01 maintains the Status column.

Every task: objective · agent · prerequisites · files · verification · acceptance · failure/recovery.

## Lanes (parallelism)

```text
P0 ─► P1 ─► P2 ─┬─► P3-T01 postgres+neo4j ─► P5 (Lane B: A04) ─────────────┐
                ├─► P3-T02 ollama+init ───► P4 (Lane A: A05 measurements+gate)│
                └─► P3-T03 embedding (A06) ┘                                 │
                     P6-T01/T02 (Lane C: A07b) ──────────────────────────────┤
                                                                              ▼
                                        P6-T03/T04 (A07a) ─► P7 ─► P8 (A08) ‖ P9–P10 (A09)
                                                                              ▼
                                                   P11 (A10) ‖ P12 (A11) ‖ P13 (A07a/A08)
                                                                              ▼
                                                        P14 ─► P15 ─► P16 ─► P17
```
`‖` = runs in parallel. A01 (reporting) and A12 (tests) are continuous from P2.

## Phase 0 — Local Environment Audit

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P0-T01 | Re-run the read-only audit (OS/CPU/RAM/disk/GPU/Docker/Compose/Git/ports/roots) and record it | A00 | — | `reports/environment-audit.md` | commands + outputs pasted | Docker daemon reachable; ports free; roots readable | Docker down → report BLOCKER (owner starts Docker Desktop) | done 2026-09-14 (re-run PASS) |

## Phase 1 — Architecture Contract

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P1-T01 | Domain models, ports, ontology loader, config; sync with `schemas/` | A02 | P0 | `packages/aimemory/{domain,ontology,common}`, `schemas/` | `pytest tests/unit/test_contracts*` | importable, tested, documented | schema/model mismatch → fix in A02 before handoff | done 2026-09-14 (157 passed, 1 skipped; container re-verification pending P2-T02) |
| P1-T02 | Architecture docs: data-model, ontology, temporal, retrieval | A02 | P1-T01 | `docs/architecture/*.md` | review by A00 | matches plan; deviations noted | — | done 2026-09-14 (docs written, deviations noted; A00 review not yet recorded) |

## Phase 2 — Repository Scaffold

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P2-T01 | Directory tree, root files, compose drafts, config, schemas, ADRs, agents, reports skeleton | A00 | — | (this scaffold) | tree listing | all planned paths exist | — | done 2026-09-13 |
| P2-T02 | Dockerfiles ×4 + tools image, lock file, `init-env`, scripts skeleton | A03 | P1-T01 | `apps/*/Dockerfile`, `infra/docker/`, `scripts/`, lock file | `docker compose config`; `docker build` each | builds succeed | build failure → pin/adjust deps, retry ≤ 3 | todo |
| P2-T03 | `git init`, `.gitattributes`, first commit of scaffold | A00 | P2-T01 | `.git` | `git log` | commit exists | — | todo (on START DEVELOPMENT) |
| P2-T04 | Test scaffolding: `conftest.py`, markers, service-availability skips | A12 | P1-T01 | `tests/conftest.py` | `pytest --collect-only` | collects cleanly | — | todo |

## Phase 3 — Core Docker Infrastructure

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P3-T01 | postgres (pgvector) + neo4j up, healthy, pinned, loopback-only | A03 | P2-T02 | compose, `infra/postgres/init`, `reports/image-pins.md` | `docker compose ps`, `netstat` | healthy; 127.0.0.1 only | port conflict → change host port in `.env`; report | todo |
| P3-T02 | ollama + `ollama-init` pulls `qwen3:4b`; pinned | A03 | P2-T02 | compose, `infra/ollama` | `ollama list` inside container | model present; digest recorded | pull failure → retry; network issue → BLOCKER | todo |
| P3-T03 | embedding-service image with baked MiniLM, offline, health | A06 | P2-T02 | `apps/embedding-service`, `providers/embedding` | health + `--network none` test | 384-d, offline | HF download failure at build → retry; report | todo |

## Phase 4 — Local AI Validation (Lane A)

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P4-T01 | OllamaProvider (`format` schema, think off, retries, identity) | A05 | P3-T02, P1-T01 | `providers/llm` | unit (mocked) + live test | port satisfied | — | todo |
| P4-T02 | Measure Qwen3 4B: load, RAM, tok/s, JSON validity ×20 | A05 | P4-T01 | `reports/local-ai-validation.md` | raw numbers with commands | MEASURED table | validity < 70 % → BLOCKER per stop rule | todo |
| P4-T03 | Measure MiniLM throughput and RAM | A05/A06 | P3-T03 | same report | raw numbers | MEASURED | — | todo |
| P4-T04 | Graphiti gate C1–C6; ADR-0009 verdict | A05 | P4-T01, P3-T01, P3-T03 | `reports/graphiti-gate.md`, ADR-0009 | per-criterion results | verdict recorded automatically | graphiti install failure counts as gate fail (recorded) | todo |

## Phase 5 — Data Model + Migrations (Lane B)

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P5-T01 | Alembic 0001: all tables, indexes, view, seeds; repositories | A04 | P1-T01, P3-T01 | `infra/postgres/alembic`, `persistence/` | up/down/up; unit + integration | clean, idempotent | migration error → fix; never hand-edit the DB | todo |
| P5-T02 | Neo4j constraints/indexes, read-only user, `GraphStore` impl, `migrate` command | A04 | P3-T01 | `infra/neo4j/schema`, `persistence/graph_store.py`, `cli/migrate.py` | constraint tests | idempotent | — | todo |
| P5-R01 | Opus review of P5 | A00 | P5-T01/02 | — | review notes | approved | findings → A04 fixes | todo |

## Phase 6 — Ingestion Engine (Lane C then core)

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P6-T01 | Policy resolver, `.memoryignore`, secret detector, fixtures | A07b | P1-T01 | `sources/policies.py`, `ignore.py`, `secrets.py`, fixtures | unit + adversarial | all adversarial safe | — | todo |
| P6-T02 | Extractors + chunkers | A07b | P1-T01 | `extractors/`, `chunking/` | unit tests; bounds on real sample | pass | pdf/docx failure → CATALOG_ONLY with reason | todo |
| P6-T03 | Discovery, path guard, fingerprint, diff, state machine, CLI | A07a | P5, P6-T01/02, P3-T03 | `apps/ingestion`, `sources/`, `cli/` | memory scenarios | pass | stage failure → job `failed`, resumable | todo |
| P6-T04 | Change-detection scenario suite | A07a + A12 | P6-T03 | `tests/memory` | pytest -m memory | all 11 scenarios | — | todo |

## Phase 7 — my-vault Ingestion (Tier 0–1)

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P7-T01 | Tier 0 registry seed from `me.md` + `project-graph.md`; aliases | A07a | P6-T03 | `sources/registry.py` | `SELECT count(*) FROM projects` vs table rows | every project row present, tracks correct | parse mismatch → report rows missed | todo |
| P7-T02 | Tier 1: chunk + embed all INDEX_CONTENT vault sources | A07a | P7-T01 | — | counts in `aimemory-ingest status` | 100 % of INDEX_CONTENT embedded | embed failure → retry batch | todo |
| P7-T03 | Structural graph projection | A08 | P7-T02, P5-T02 | `knowledge/structural.py`, `providers/graph` | Cypher counts | projects/docs/links/technologies present | — | todo |

## Phase 8 — Temporal Graph

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P8-T01 | KnowledgeEngine port + temporal rules (ADR-0005) + provenance builders | A08 | P1-T01, P5 | `knowledge/temporal`, `provenance/` | unit tests | rules verified | — | todo |
| P8-T02 | Engine implementation per ADR-0009 (Graphiti or native) | A08 | P4-T04, P8-T01 | `knowledge/{graphiti_engine,native_engine}` | fixture episodes | ExtractionResult valid; Postgres mirror | native JSON failures → retry then `failed`; gate already decided engine | todo |
| P8-T03 | Entity resolution (deterministic-first) | A08 | P8-T02 | `knowledge/entity_resolution` | unit + fixture | aliases resolve; no duplicate Projects | — | todo |
| P8-T04 | Tier 2 extraction run on my-vault (background, priority order) | A07a/A08 | P8-T02/03 | — | `status` coverage | priority tiers complete; rest queued | slow → continue in background; report coverage | todo |

## Phase 9 — Vector Retrieval

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P9-T01 | Vector + keyword candidates + RRF + boosts | A09 | P7-T02 | `retrieval/` | unit + integration on Tier-1 data | top-k sane on smoke queries | — | todo |

## Phase 10 — Memory Gateway

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P10-T01 | Graph expansion, temporal filter, provenance, context assembly | A09 | P9-T01, P8-T01 | `retrieval/`, `gateway/` | integration | citations + validity present | Neo4j down → vector-only + warning | todo |
| P10-T02 | memory-api REST, health, metrics, logs; OpenAPI export | A09 | P10-T01 | `apps/memory-api`, `schemas/api/openapi.json` | contract tests | healthy in compose | — | todo |

## Phase 11 — MCP

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P11-T01 | MCP server: tools/resources, safeguards, audit, stdio launcher, docs | A10 | P10-T02 | `apps/mcp-server`, `scripts/mcp-stdio.*`, `docs/operations/mcp.md` | SDK client | all tools; writes guarded | — | todo |
| P11-T02 | MCP client test suite | A12 | P11-T01 | `tests/integration/test_mcp_*` | pytest | tool list == `tools.json` | — | todo |

## Phase 12 — Visualization

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P12-T01 | NeoDash dashboard + saved queries + read-only verification | A11 | P7-T03, P8 | `infra/neodash`, `docs/operations/queries.md` | open pages | render with data | NeoDash image issue → fall back to Neo4j Browser queries only, record limitation | todo |
| P12-T02 | Ops dashboard `/ops` (ADR-0011): tables `run_requests`, `service_stats` (A04, migration 0002 with P14-T05 tables), worker mode `aimemory-ingest worker` + progress + snapshots (A07a), page + actions + review form (A16), router mount (A09) | A16 lead; A04, A07a, A09 | P10-T02, P8-T04 | `packages/aimemory/ops`, `apps/memory-api/{templates,static,routes/ops.py}`, `apps/ingestion` worker, compose `ingestion` service | click every button; worker completes; verdicts persist | integration tests pass; works offline | worker failure → CLI path still works; record | todo |

## Phase 13 — First Deep Project (JobLab DE)

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P13-T01 | Tier 0–2 ingest of `joblab-de` root (docs first) | A07a | P8-T04 | config | `status` coverage | docs/, README, src/, tests/ indexed; JDK/tools-cache ignored | slow → priority paths first, rest background | todo |
| P13-T02 | Pilot graph validation (decisions from decision docs, provenance) | A08 | P13-T01 | `reports/pilot-validation.md` | Cypher + SQL | decisions present with provenance | gaps → record as limitation | todo |

## Phase 14 — Full Testing

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P14-T01 | Unit + integration full run | A12 | all | `reports/test-summary.md` (via A01) | raw output | green or listed | failures → owners (P14-T04) | todo |
| P14-T02 | Memory scenario suite | A12 | P6-T04 | — | pytest -m memory | green | — | todo |
| P14-T03 | Gold-set evaluation | A12/A09 | P10, P8-T04 | `tests/evaluation/gold.yaml`, `reports/evaluation-*.md` | run_eval | MEASURED metrics | — | todo |
| P14-T04 | Fix loop | owners | P14-T01..03 | — | re-run | green or honest limitation | ≤ 3 loops, then record | todo |
| P14-T05 | Evaluation protocol (ADR-0010): `metrics_snapshots` + `extraction_reviews` tables (A04), `aimemory-ingest review` + metrics writer (A07a), frozen benchmark set + `scripts/eval --benchmark` (A12), NeoDash Health page (A11) | A12 lead; A04, A07a, A11 | P8-T04, P12-T01 | `tests/evaluation/benchmark/`, `scripts/eval*`, migration 0002, `infra/neodash` | run review on 20 samples; run benchmark once with `qwen3:4b` | first benchmark report + first snapshot exist | — | todo |

## Phase 15 — Security Review

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P15-T01 | Checklist verification + threat model + fixes | A13 | P14 | `docs/security/*` | commands + results | pass or listed | fixes via owners | todo |

## Phase 16 — Final Local Deployment

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P16-T01 | Clean-start under `ai-memory-verify`, then real `docker compose up -d`; all healthy | A03 + A15 | P15 | `reports/deploy-verification.md` | `docker compose ps` | all required healthy | — | todo |
| P16-T02 | Fresh-machine doc walkthrough | A00 | P16-T01, docs | — | follow docs only | succeeds | doc gaps → A14 | todo |

## Phase 17 — Final Reporting

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P17-T01 | final-build-report, test-summary, architecture-state, known-limitations, next-steps | A01 + A02/A15 inputs | all | `reports/*`, `docs/architecture/architecture-state.md` | review | complete, truthful | — | todo |
| P17-T02 | Final commit; present the system | A00 | P17-T01 | — | `git log` | done | — | todo |
