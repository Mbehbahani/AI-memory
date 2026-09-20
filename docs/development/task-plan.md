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
| P2-T02 | Dockerfiles ×4 + tools image, lock file, `init-env`, scripts skeleton | A03 | P1-T01 | `apps/*/Dockerfile`, `infra/docker/`, `scripts/`, lock file | `docker compose config`; `docker build` each | builds succeed | build failure → pin/adjust deps, retry ≤ 3 | done (reconstructed-from-git) 2026-09-14 — commit `0e65d6d`; commit message asserts all five images build; not independently re-verified by A01. See `reports/image-pins.md` |
| P2-T03 | `git init`, `.gitattributes`, first commit of scaffold | A00 | P2-T01 | `.git` | `git log` | commit exists | — | done (reconstructed-from-git) — first commit `01337bfdcdf776c92c3f39baa9d2d3404cd1fe57`, 2026-09-14 |
| P2-T04 | Test scaffolding: `conftest.py`, markers, service-availability skips | A12 | P1-T01 | `tests/conftest.py` | `pytest --collect-only` | collects cleanly | — | done (reconstructed-from-git) — `tests/conftest.py` present (commit `1562ba9`); no isolated collect-only run recorded, but the 2026-09-15 measured full-suite run (428 passed, 1 skipped) collected the whole tree cleanly |

## Phase 3 — Core Docker Infrastructure

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P3-T01 | postgres (pgvector) + neo4j up, healthy, pinned, loopback-only | A03 | P2-T02 | compose, `infra/postgres/init`, `reports/image-pins.md` | `docker compose ps`, `netstat` | healthy; 127.0.0.1 only | port conflict → change host port in `.env`; report | done 2026-09-14 (verified by A00; see reports/infra-bringup.md) |
| P3-T02 | ollama + `ollama-init` pulls `qwen3:4b`; pinned | A03 | P2-T02 | compose, `infra/ollama` | `ollama list` inside container | model present; digest recorded | pull failure → retry; network issue → BLOCKER | done 2026-09-14 (verified by A00; digest 359d7dd4bcda) |
| P3-T03 | embedding-service image with baked MiniLM, offline, health | A06 | P2-T02 | `apps/embedding-service`, `providers/embedding` | health + `--network none` test | 384-d, offline | HF download failure at build → retry; report | done 2026-09-14 (code A06, verified by A00; provider unit/integration tests still owed) |

## Phase 4 — Local AI Validation (Lane A)

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P4-T01 | OllamaProvider (`format` schema, think off, retries, identity) | A05 | P3-T02, P1-T01 | `providers/llm` | unit (mocked) + live test | port satisfied | — | done (reconstructed-from-git) 2026-09-14 — commit `606d8a3`; passing in the 2026-09-15 428-passed/1-skipped run |
| P4-T02 | Measure Qwen3 4B: load, RAM, tok/s, JSON validity ×20 | A05 | P4-T01 | `reports/local-ai-validation.md` | raw numbers with commands | MEASURED table | validity < 70 % → BLOCKER per stop rule | done — n=20 both providers, MEASURED 2026-09-14/15 (commits `1562ba9`, `a193e18`); both clear the 70% stop rule (95.0% each); qwen3:4b relationship recall 40.2% fails plan §O C3's ≥50% bar, Haiku 61.2% passes — feeds ADR-0009/ADR-0014 |
| P4-T03 | Measure MiniLM throughput and RAM | A05/A06 | P3-T03 | same report | raw numbers | MEASURED | — | done — MEASURED (30.7 texts/s @256-batch, p50 14.2ms/p95 21.1ms single-text); commit `a193e18`, report states "P4-T03 is satisfied" |
| P4-T04 | Graphiti gate C1–C6; ADR-0009 verdict | A05 | P4-T01, P3-T01, P3-T03 | `reports/graphiti-gate.md`, ADR-0009 | per-criterion results | verdict recorded automatically | graphiti install failure counts as gate fail (recorded) | done — **verdict recorded (ADR-0009, native engine), but the live gate itself produced no clean result**: crashed on episode E01 (SDK incompatibility), C1/C3/C4/C5 unmeasured, C6 partial. Verdict rests on independent P4-T02 arithmetic (Ollama branch fails C2 by 4.4–7.3×), not the gate run. See `reports/graphiti-gate.md`, commit `8d54ebc`. Neo4j has uncleaned Graphiti schema residue (24 indexes/18 constraints) — A08/A11 must diff before dropping anything |

## Phase 5 — Data Model + Migrations (Lane B)

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P5-T01 | Alembic 0001: all tables, indexes, view, seeds; repositories | A04 | P1-T01, P3-T01 | `infra/postgres/alembic`, `persistence/` | up/down/up; unit + integration | clean, idempotent | migration error → fix; never hand-edit the DB | done 2026-09-14 (28 tables + provenance_v; 21/21 tests) |
| P5-T02 | Neo4j constraints/indexes, read-only user, `GraphStore` impl, `migrate` command | A04 | P3-T01 | `infra/neo4j/schema`, `persistence/graph_store.py`, `cli/migrate.py` | constraint tests | idempotent | — | done 2026-09-14 (18 constraints; read-only user NOT enforceable — ADR-0013) |
| P5-R01 | Opus review of P5 | A00 | P5-T01/02 | — | review notes | approved | findings → A04 fixes | done 2026-09-14 — APPROVED; one finding raised to ADR-0013 |

## Phase 6 — Ingestion Engine (Lane C then core)

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P6-T01 | Policy resolver, `.memoryignore`, secret detector, fixtures | A07b | P1-T01 | `sources/policies.py`, `ignore.py`, `secrets.py`, fixtures | unit + adversarial | all adversarial safe | — | done (reconstructed-from-git) 2026-09-14 — commit `606d8a3`; passing in the 2026-09-15 428-passed/1-skipped run |
| P6-T02 | Extractors + chunkers | A07b | P1-T01 | `extractors/`, `chunking/` | unit tests; bounds on real sample | pass | pdf/docx failure → CATALOG_ONLY with reason | done (reconstructed-from-git) — built `606d8a3`; a real-vault bounds test failed (264-token chunk vs 250-token assertion, see `reports/test-summary.md`) until fixed in `8e495ca`; passing in the 428-passed/1-skipped run |
| P6-T03 | Discovery, path guard, fingerprint, diff, state machine, CLI | A07a | P5, P6-T01/02, P3-T03 | `apps/ingestion`, `sources/`, `cli/` | memory scenarios | pass | stage failure → job `failed`, resumable | done (reconstructed-from-git) — partial in `1562ba9` (A07a cut off by rate limit), completed in `2c87f86`; memory scenarios green (428 passed, 1 skipped, measured 2026-09-15) |
| P6-T04 | Change-detection scenario suite | A07a + A12 | P6-T03 | `tests/memory` | pytest -m memory | all 11 scenarios | — | done — commit `2c87f86`; two scenarios required fixes to reach green (see `reports/test-summary.md`); MEASURED 2026-09-15 in the `tools` image: 428 passed, 1 skipped |

## Phase 7 — my-vault Ingestion (Tier 0–1)

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P7-T01 | Tier 0 registry seed from `me.md` + `project-graph.md`; aliases | A07a | P6-T03 | `sources/registry.py` | `SELECT count(*) FROM projects` vs table rows | every project row present, tracks correct | parse mismatch → report rows missed | done — commit `37b90da`. MEASURED against `D:\My-Vault` (read-only, confirmed): 27 projects = 7 from `me.md` + 21 from `project-graph.md` − 1 merged on slug — acceptance met exactly. Also fixed while landing this: a registry-parsing bug that filed 3 research projects under `business` (substring match on project-track commentary) and path-comment aliasing that mis-filed documents (see `37b90da`) |
| P7-T02 | Tier 1: chunk + embed all INDEX_CONTENT vault sources | A07a | P7-T01 | — | counts in `aimemory-ingest status` | 100 % of INDEX_CONTENT embedded | embed failure → retry batch | done — commit `37b90da`. MEASURED 98.6 % (143/145 INDEX_CONTENT sources chunked+embedded), **not rounded up to 100 %**: 176 sources / 2,838 chunks / 2,779 embeddings, reproducible across three runs with identical counters, zero LLM and zero cloud calls. The two shortfalls are settled outcomes, not gaps: one 192-byte frontmatter-only file correctly yields 0 chunks; one file is byte-identical to another and its text is embedded under its twin. Restartability proved by killing and resuming mid-scan (0/29 pre-kill versions changed chunk count on resume). Two chunker defects found on this same run (fence-swallowing prose, single-line-over-budget) fixed separately in `78561f3` — see that row's note below |
| P7-T03 | Structural graph projection | A08 | P7-T02, P5-T02 | `knowledge/structural.py`, `providers/graph` | Cypher counts | projects/docs/links/technologies present | — | **todo — still open.** Code exists (`packages/aimemory/knowledge/structural.py`, `providers/graph/{projection,writer}.py`, commit `2c87f86`) but Neo4j holds **0 nodes** (MEASURED) — no projection has ever run against real data, so graph expansion in retrieval has nothing to expand and the `entity_linked` boost cannot fire. `aimemory-ingest rebuild-graph` fails: the CLI imports `knowledge.structural.rebuild_graph`, which does not exist — `structural.py` exports `StructuralIndex`, `structural_plan`, `document_node_id`, but no driver function to actually run the projection and write to Neo4j. Remains todo until that function is written and a real run is measured |

## Phase 8 — Temporal Graph

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P8-T01 | KnowledgeEngine port + temporal rules (ADR-0005) + provenance builders | A08 | P1-T01, P5 | `knowledge/temporal`, `provenance/` | unit tests | rules verified | — | done (reconstructed-from-git) — commit `2c87f86` (recovered after an unrecorded power loss); exercised via `tests/memory` scenario fixtures, green in the 2026-09-15 428-passed/1-skipped run |
| P8-T02 | Engine implementation per ADR-0009 (Graphiti or native) | A08 | P4-T04, P8-T01 | `knowledge/{graphiti_engine,native_engine}` | fixture episodes | ExtractionResult valid; Postgres mirror | native JSON failures → retry then `failed`; gate already decided engine | done (reconstructed-from-git) — native engine built per ADR-0009, commit `2c87f86`; `graphiti_engine/` kept empty as documented extension point |
| P8-T03 | Entity resolution (deterministic-first) | A08 | P8-T02 | `knowledge/entity_resolution` | unit + fixture | aliases resolve; no duplicate Projects | — | done (reconstructed-from-git) — commit `2c87f86`; exercised via `tests/memory` scenario fixtures |
| P8-T04 | Tier 2 extraction run on my-vault (background, priority order) | A07a/A08 | P8-T02/03 | — | `status` coverage | priority tiers complete; rest queued | slow → continue in background; report coverage | **partial — 15 of 144 episodes, not complete.** Commit `c462170` fixed the wiring that had blocked any run at all: `knowledge/__init__.py` was a bare docstring, so `from ..knowledge import get_engine` raised `ImportError`, caught by a broad `except` and reported as "not built yet" even though the engine existed; `run_tier2` also only persisted `if writer is not None` and every CLI path passed `None`, so a wired engine with no writer would have called the LLM for every episode and discarded every result. Both fixed (`get_engine`/`get_writer` factories added; `run_tier2` now refuses to run without a writer). With wiring fixed, a deliberately scoped pilot ran: 15 non-sensitive technical episodes (Bedrock Haiku 4.5), **0 failed**, 199.6 s total, ~13.3 s/episode MEASURED (2–3 LLM calls/episode). Produced 129 entities, 147 facts, 81 artifacts, 145 mentions, 0 facts with incomplete provenance. Predicates: USES 35, RELATED_TO 31, PART_OF 22, USES_ARCHITECTURE 17, HAS_OWNER 14, DEPLOYED_ON 14. The pilot deliberately excluded `06 Outputs/Career Docs`, `06 Outputs/Motivation Letters`, `00 Inbox/job-positions`, `02 Areas/Career Development`, `AIOS/me.md`, `skill-map`, `INTERVIEW_COACH` (episode priorities temporarily altered to control selection, then restored; verified afterwards via `facts.source_uri` that none of those reached the model). The other 129 episodes remain `pending` — full background run not yet done |

## Phase 9 — Vector Retrieval

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P9-T01 | Vector + keyword candidates + RRF + boosts | A09 | P7-T02 | `retrieval/` | unit + integration on Tier-1 data | top-k sane on smoke queries | — | done — commit `ef30037` (code), `ff19689` (doc correction). MEASURED in the tools container: 518 passed, 2 skipped (+90 tests over prior); mypy clean on all 8 retrieval modules; end-to-end `retrieve()` over a 16-chunk fixture corpus with real MiniLM vectors, median 16.8–17.2 ms across 3 queries × 5 samples. Deliberate deviation, documented in code and a test: `since` filters on `source_versions.observed_at` (observation axis), not `chunks.created_at` — `docs/architecture/retrieval.md` §1 had the wrong axis and an invalid SQL JOIN in §2 (confirmed against the live DB: "invalid reference to FROM-clause entry for table c"); both corrected in `ff19689`, code untouched (code was right, doc was wrong). Retrieval quality (recall/precision) was UNKNOWN at commit time — no ingested Tier-1 corpus existed yet; real numbers now possible after P7-T02 but not yet measured under P9 |

## Phase 10 — Memory Gateway

| ID | Objective | Agent | Prereq | Files | Verification | Acceptance | Failure / recovery | Status |
|---|---|---|---|---|---|---|---|---|
| P10-T01 | Graph expansion, temporal filter, provenance, context assembly | A09 | P9-T01, P8-T01 | `retrieval/`, `gateway/` | integration | citations + validity present | Neo4j down → vector-only + warning | done — commit `43d8a2b`. MEASURED: 107 passed in the retrieval/gateway subset (run without the destructive migration test); mypy clean on `apps/memory-api` and owned packages. Degradation proved by simulated outage (a graph double whose every call raises `ConnectionError` still returns cited hits with complete provenance, invents no `entity_linked` boost, records the warning in both the response and `retrieval_logs`), not asserted by docstring. `as_of` reads now filter on the validity window only — an earlier `current_status <> 'superseded'` clause was removed because it hid artifacts that were genuinely current at an earlier `as_of`, defeating a point-in-time read. Note: with Neo4j at 0 nodes (P7-T03 still open), graph expansion currently has nothing to expand in practice, though the code path and its degraded-mode handling are built and tested |
| P10-T02 | memory-api REST, health, metrics, logs; OpenAPI export | A09 | P10-T01 | `apps/memory-api`, `schemas/api/openapi.json` | contract tests | healthy in compose | — | done — commit `43d8a2b`. **memory-api reaches healthy for the first time.** Root cause of the prior restart loop: placeholder `CMD uvicorn apps.memory_api.main:app` could never import — the directory is `apps/memory-api` and a hyphen cannot appear in a Python module path; fixed the way `embedding-service` already does it. MEASURED: OpenAPI export 14 paths, regeneration enforced by `--check`. Loopback decision recorded: uvicorn binds `0.0.0.0` *inside* the container; the guarantee is the published port (`127.0.0.1:8000`) per ADR-0007; a new test parses both compose files and fails if any service publishes on a non-loopback interface. `/health` returns 200 whenever Postgres is reachable; Neo4j or the embedder being down is reported "degraded", not "down" |

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
