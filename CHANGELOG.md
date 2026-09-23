# Changelog

All notable changes to this project are documented here. Format: Keep a Changelog. Versioning: SemVer.

## [Unreleased]

### Added
- 2026-09-13 — Repository scaffold: directory tree, plan (`docs/architecture/v0.1-plan.md`), ADR-0001…0008,
  agent definitions (`.claude/agents/`), task plan, contracts (`schemas/`), config templates, Compose
  topology draft, reporting skeleton. No services built or started.
- 2026-09-14 — ADR-0010 ongoing evaluation protocol (metrics snapshots, human review sampling, frozen
  benchmark set, model-upgrade rule) and plan §AF; task P14-T05.

- 2026-09-14 — **START DEVELOPMENT.** P0-T01 environment re-audit (A00): Docker daemon reachable
  (server 29.7.2, Compose v5.4.0, 8 CPU / 15.46 GiB VM), 0 containers/images at start, all 8 planned
  ports free, both source roots readable. Verdict PASS, no blockers (`reports/environment-audit.md`).
- 2026-09-14 — P1-T01/P1-T02 (A02, Opus): architecture contracts implemented — pydantic v2 domain
  models, ports (runtime-checkable Protocols), ontology loader, config, hashing/id/logging/time
  helpers under `packages/aimemory/{domain,common,ontology}`; ontology loader synced with
  `schemas/ontology.yaml`; `schemas/mcp/tools.json` rewritten with full per-tool JSON-Schema
  `inputSchema` (10 read + 2 write tools, unchanged from ADR-0008); architecture docs written:
  `docs/architecture/{data-model,ontology,temporal,retrieval}.md`. 157 unit tests passing, 1 skipped
  (POSIX-only), ruff and mypy clean (`python -m pytest tests/unit -q`, host `.venv-contracts`).

- 2026-09-14 — P2-T02 (A03): 4 service Dockerfiles + tools image, two dependency lock files pinned to
  Python 3.12, image digests recorded (`reports/image-pins.md`), 9 operator scripts
  (init-env/up/down/doctor/ingest/backup/restore/rebuild-graph/test). All five images build.
  P2-T03: `git init` and first commit of the scaffold.
- 2026-09-14 — P3 core infrastructure (A03/A06 code, A00 direct verification after all four background
  agents were terminated mid-run by a session limit): Postgres 17 + pgvector healthy with `pg_trgm`,
  `pgcrypto`, `vector`; Neo4j 5.26 Community healthy with 190 APOC procedures, 512M heap / 256M page
  cache; `qwen3:4b` pulled (2.5 GB, digest `359d7dd4bcda`) and producing schema-valid JSON via Ollama
  `format=`; embedding-service serving 384-d MiniLM vectors offline (`--network none` proven), 30.7
  texts/s on a 256-text batch. Every published port confirmed bound to `127.0.0.1` only (ADR-0007
  holds). See `reports/infra-bringup.md`.
- 2026-09-14 — P5 (A04): Alembic migrations 0001+0002 — 28 tables + `provenance_v` view, HNSW cosine
  index on `embeddings.vector(384)`, partial unique index `uq_facts_functional_current` enforcing
  ADR-0005 rule 1 in the database; SQLAlchemy repositories; `Neo4jGraphStore` with ontology edge
  validation; `aimemory-ingest migrate`. Upgrade/downgrade/upgrade verified clean against live
  Postgres. P5-R01 (A00, Opus review): **APPROVED** — independently re-verified the functional-uniqueness
  constraint, the `valid_to >= valid_from` window check, and the 25-value predicate CHECK directly
  against the live database.
- 2026-09-14 — ADR-0012: two `LLMProvider` implementations built and measured side by side —
  `OllamaProvider` (`qwen3:4b`, local, offline, unchanged) and a new `BedrockProvider` (Claude Haiku
  4.5 via US inference profile `us.anthropic.claude-haiku-4-5-20251001-v1:0`, forced tool use for
  strict JSON since Bedrock has no `format=` equivalent). Neither privileged as reference at this
  point; owner decision to build and compare both.
- 2026-09-14 — P6-T01/T02 (A07b): policy resolver, `.memoryignore`, secret detector, adversarial
  fixtures; extractors for markdown/code/pdf/docx/notebook/tex/structured/plaintext; markdown and
  code chunkers.
- 2026-09-15 — P4-T02 (A05 data, A00 write-up), measurement campaign complete for **both** providers,
  n=20 real my-vault episodes each against frozen schemas (`reports/local-ai-validation.md`):
  first-pass JSON validity **tied at 95.0 %**; mean entity recall **tied at 68.9 %**; mean
  relationship recall `qwen3:4b` **40.2 %** vs Haiku **61.2 %** (plan §O C3 requires ≥50 % —
  local model **fails** the plan's own bar, Bedrock passes); median seconds/episode 174.43 (ollama)
  vs 7.74 (bedrock); Ollama RSS with the model resident MEASURED at median 8.01 GiB / p90 10.82 GiB /
  **max 11.37 GiB** (roughly 2× the plan's 4–5.5 GB estimate — retroactively validates declining
  AC-9's 12 GB WSL2 cap); Bedrock cost MEASURED at $0.0083/episode, ~$1.66 extrapolated for the vault.
  Graphiti gate (P4-T04) produced **no clean verdict** — the Bedrock-only attempt crashed on episode
  E01 (`TypeError: AsyncMessages.create() got an unexpected keyword argument 'temperature'`, a
  `graphiti-core`/`anthropic` SDK incompatibility) — but the Ollama branch is independently settled by
  arithmetic on the P4-T02 measurements: Graphiti's 6–10 calls/episode at 174.43 s median = 1,047–1,744 s
  against C2's 240 s threshold, a 4.4×–7.3× failure, exactly as plan §A.2 predicted. Graphiti left 24
  indexes and 18 constraints as uncleaned schema residue in the live Neo4j (no data nodes were
  created) — must be diffed against `infra/neo4j/schema/constraints.cypher` before anything is
  dropped.
- 2026-09-15 — **ADR-0009**: the native temporal engine is adopted for V0.1, not Graphiti — decided on
  the independent P4-T02 arithmetic above, explicitly not on the crashed gate run. `graphiti_engine/`
  remains an empty package as a documented extension point.
- 2026-09-15 — **ADR-0014**: Claude Haiku 4.5 (Bedrock) becomes the **default** extraction provider
  (discriminator: relationship recall, 61.2 % vs 40.2 %, against the plan's own ≥50 % bar); `qwen3:4b`
  is retained as the offline/zero-cost mode, not deprecated. Mismatch prevention made structural: one
  extraction model per corpus — the pipeline refuses to extract into a project whose current facts came
  from a different `extraction_model_id`, with an explicit re-extraction (ADR-0005 supersession) as the
  only remedy; `--allow-model-mix` exists solely for benchmarking and is recorded on the run.
  Deterministic Tier 0 seeding (PARA folder names, registry projects) now wins over both models'
  systematic mistyping of the same entities, with entity type rejected at write time when it
  contradicts a seed. **Cost, stated plainly**: assumption B16 no longer holds by default — vault
  content, including CVs and job applications, is sent to AWS Bedrock during extraction; AC-8
  ("nothing leaves the machine") now holds only under `LLM_PROVIDER=ollama`.
- 2026-09-15 — P6-T03/T04 and P8-T01/T02/T03 (A07a, A08; recovered after an unrecorded machine power
  loss and completed in commit `2c87f86`): discovery, path guard, fingerprint, diff, ingestion state
  machine, and the `aimemory-ingest` CLI; the `KnowledgeEngine` port and ADR-0005 temporal rules;
  the native extraction engine (ADR-0009); deterministic-first entity resolution; provenance builders
  and an explain view; the structural graph projection and Neo4j writer. The 11-scenario
  change-detection suite (`tests/memory`) is green. **Full suite measured 2026-09-15 in the `tools`
  image: 428 passed, 1 skipped** (`tests/unit/test_contracts_uri.py:206`, Windows junction reparse
  points — a platform skip, not a failure).

- 2026-09-17 — P9-T01 (A09): hybrid retrieval — vector + keyword candidates, RRF fusion, boosts
  (`packages/aimemory/retrieval/{candidates,fusion,boosts,pipeline,config,filters,types}.py`).
  MEASURED: 518 passed, 2 skipped; mypy clean on all 8 retrieval modules; end-to-end `retrieve()`
  over a 16-chunk fixture corpus with real MiniLM vectors, median 16.8–17.2 ms across 3 queries ×
  5 samples. Deliberate, documented deviation: `since` is evaluated on `source_versions.observed_at`
  (observation axis), not `chunks.created_at`.
- 2026-09-17 — P10-T01/T02 (A09): graph expansion, temporal filter, provenance, context assembly
  (`packages/aimemory/retrieval/{context,expansion,logs,provenance,temporal}.py`,
  `packages/aimemory/gateway/*`); `memory-api` REST — health, metrics, logs, OpenAPI export
  (`apps/memory-api/*`, `schemas/api/openapi.json`). **`memory-api` reaches healthy for the first
  time** — the previous restart loop was a placeholder CMD that could never import (`apps/memory-api`'s
  hyphen is illegal in a Python module path). MEASURED: 107 passed in the retrieval/gateway subset;
  OpenAPI 14 paths, `--check` enforced. Degradation proved by simulated outage (Neo4j `ConnectionError`
  still returns cited hits, no invented boost, warning recorded); `/health` treats Neo4j/embedder-down
  as "degraded", not "down".
- 2026-09-17 — P7-T01/T02 (A07a): **first real vault ingest.** MEASURED against `D:\My-Vault`
  (read-only, confirmed): 176 sources registered (143 INDEX_CONTENT, 2 MIRROR, 31 CATALOG_ONLY),
  2,838 chunks, 2,779 embeddings, 27 projects (7 from `me.md` + 21 from `project-graph.md` − 1 merged
  on slug — P7-T01 acceptance met exactly), 64 aliases, 35 deterministic entities, 2m33s, reproducible
  across three runs with identical counters, **zero LLM and zero cloud calls**. P7-T02 coverage is
  **98.6% (143/145), not rounded up to 100%** — both shortfalls are settled outcomes (one frontmatter-
  only file correctly yields 0 chunks; one file is byte-identical to another and embedded under its
  twin). Six ingestion bugs fixed in the process, including a `--tier 0` then `--tier 1` sequence that
  produced nothing (tiers are now a ceiling *and* a debt) and a CATALOG_ONLY downgrade that was rolled
  back by the very exception that triggered it. Restartability proved by killing and resuming mid-scan.
- 2026-09-17 — P8-T02 wiring fix (A08): `aimemory-ingest tier2` reported "not built yet" although the
  native engine was already committed — `knowledge/__init__.py` was a bare docstring, masked by a
  broad `except`. Added `get_engine`/`get_writer` factories; `run_tier2` now refuses to run without a
  writer (previously it would have called the LLM for every episode and discarded every result).
  Enabled a deliberately scoped **P8-T04 pilot**: 15 of 144 queued episodes (Bedrock Haiku 4.5),
  0 failed, 199.6s total (~13.3 s/episode), 129 entities / 147 facts / 81 artifacts / 145 mentions,
  0 facts with incomplete provenance. Scoped to exclude sensitive personal/career paths; verified via
  `facts.source_uri` that none reached the model. 129 episodes remain `pending`.
- 2026-09-17 — Full suite MEASURED by the orchestrator: **627 passed, 1 skipped**
  (`docker compose --profile tools run --rm tools pytest -q`), superseding the 2026-09-15 figure of
  428 passed, 1 skipped. The one skip is the Windows-junction test; the former "no Tier-1 corpus" skip
  is gone because a real corpus now exists (P7-T02).

### Changed
- 2026-09-22 — P12-T02 Ops dashboard timestamp-localization follow-up (A16): use the existing
  `fmt_amsterdam_time` Jinja filter for actual timestamp instants across the health, notes, quality,
  and review templates; date-only values remain unchanged. Focused integration validation reported
  26 passed, 2 warnings, exit 0.
- 2026-09-17 — Bedrock credentials wired into containers (A03, commit `755b06f`): optional read-only
  `~/.aws` bind mount (`HOST_AWS_DIR`, defaults to a checked-in empty dir), `AWS_PROFILE=mohabehb`,
  `us-east-1`, and the `bedrock` extra added to the ingestion and tools images. Before this, no compose
  service passed AWS credentials and neither image installed the `bedrock` extra, so `LLM_PROVIDER=bedrock`
  (the ADR-0012/ADR-0014 default) could not authenticate from any container — boto3 was absent entirely.
- 2026-09-17 — `docs/architecture/retrieval.md` corrected against the shipped P9-T01 code (A02, commit
  `ff19689`): §1's `since` filter used ingestion time (`chunks.created_at`) instead of the observation
  axis (`source_versions.observed_at`), contradicting §5 and `temporal.md`; §2's keyword-search SQL
  was invalid (`c` out of scope in its own `ON` clause), confirmed against the live database. The code
  was right; the doc was wrong.
- 2026-09-14 — Plan §AC rewritten as a decision table (AC-9 now "cap WSL2 at 12 GB", AC-11 added);
  frugal-RAM defaults: `OLLAMA_KEEP_ALIVE=5m`, Neo4j heap 512M / page cache 256M; idle-profile
  expectations added to §Z.
- 2026-09-14 — ADR-0011 offline Ops dashboard (`/ops`) + always-on ingestion worker; agent A16 `ops-dashboard`; task P12-T02; compose `ingestion` service is now a worker (no `ingest` profile).
- 2026-09-14 — `schemas/ontology.yaml`: `SubProject` now carries `stored_as: Project` (19 node types
  stored as 18 Neo4j labels); property-set comments converted to real `node_properties`/
  `relationship_properties` keys with `engine`/`observed_at` required (A02, P1-T01).
- 2026-09-14 — **ADR-0013** amends plan §T/§Q/§S: Neo4j Community Edition cannot enforce a read-only
  user (`GRANT ROLE`/`SHOW ROLES` → "Unsupported administration command"; a role-less user
  successfully ran `CREATE`, MEASURED). `readonly-user.cypher`'s non-functional `GRANT ROLE` line
  removed. Replaced with three real layers: client-side write refusal in `Neo4jGraphStore.query()`,
  the graph's rebuildability (ADR-0001), and loopback-only exposure (ADR-0007). NeoDash is called out
  as unconstrained by this boundary (it bypasses `GraphStore`).

### Fixed
- 2026-09-17 — `docker compose up -d` could never complete (A03, commit `3b80de9`): `apps/ingestion/Dockerfile`
  never `COPY`ed `infra/postgres/alembic` or `infra/neo4j/schema`, so `cli/migrate.py` failed with
  "No 'script_location' key found in configuration" on every attempt. Fixed by copying both trees into
  the image; `migrate.py`'s own path logic was already correct.
- 2026-09-17 — Test suite destroyed the ingested vault corpus (A12, commit `670f516`): `test_migrations.py`
  ran `alembic downgrade base` against the shared dev database; two agents independently lost the real
  vault corpus to a plain `pytest -q` run (evidence: the `sources` table's OID changed across a run
  while `pg_stat_user_tables.n_tup_del` stayed 0 — a DROP + CREATE, not a DELETE). Fixed by running the
  whole module against a throwaway database created per module and dropped at teardown. A second bug
  found while verifying: the scratch-DB fixture's `str(url)` masked the password as `***`
  (SQLAlchemy's `URL.__str__()`), failing authentication for every test using that DSN — fixed with
  `hide_password=False`.
- 2026-09-17 — Two chunker defects bounded (A07b, commit `78561f3`), both MEASURED on the real vault:
  a fence-open transition in the markdown chunker never flushed the buffer, so prose absorbed an
  entire fenced block, producing a 28,446-char (~8,366-token) chunk against a 200-token target; and
  the code chunker could never cut below one whole physical line, so a one-line 12,623-char JSON file
  became one chunk that the embedding service rejected with `422 texts[0] exceeds 8000 characters`.
  Both fixed. Remaining limitation, by design: a standalone fence larger than the budget is still one
  oversized chunk (module rule 1) — measured at 10,191 chars, still above the 8,000-char embed limit.
- 2026-09-14 — P6-T02 chunking bug (A07b/fix in `8e495ca`): the markdown chunker's hard-split path
  computed its slice size once from the full `max_tokens` budget rather than from the buffer's
  remaining headroom, so a long unbroken run of characters (observed on a real-vault LinkedIn
  tracking URL) could add a full extra `max_tokens` worth of content on top of whatever was already
  buffered — producing a 264-token chunk against a 250-token test assertion. Fixed by recomputing the
  slice size from the remaining token budget before every cut.
- 2026-09-15 — Two `tests/memory` scenarios fixed while recovering in-flight P6-T04 work (`2c87f86`):
  (1) a fixture edit that appended a bare sentence collided with the extractor's frontmatter-stripped
  single-chunk output, making embedding reuse unobservable — fixed by appending a new section instead,
  which places a chunk boundary; (2) an unordered `.first()` on `metrics_snapshots` picked the wrong
  generation's row when asserting `--allow-model-mix` behaviour — fixed to check both rows, and the
  test's closing assertion was corrected to match actual system behaviour (see Known limitations)
  rather than loosened to hide it.

### Known limitations
- 2026-09-15 — `extraction_models_in_use` (and the `aimemory-ingest status` warning built on it)
  under-reports after an `--allow-model-mix` run: when a second model's proposed facts are identical
  to already-open facts from a prior model, ADR-0005 re-confirmation (`FactRepo.touch`) does not
  restamp `extraction_model_id`, so a corpus that two models actually ran over is reported as having
  one. The per-`(run, model)` `metrics_snapshots` rows do correctly show both. Surfaced and documented
  (see `tests/memory/test_change_detection.py::test_allow_model_mix_is_the_only_way_past_the_guard`
  and `reports/test-summary.md`) rather than silently worked around; closing it is an ADR decision for
  A04/A08.
- 2026-09-17 — **Neo4j holds 0 nodes.** All knowledge is Postgres-only at HEAD; graph expansion
  (P10-T01) has nothing to expand on real data and the `entity_linked` boost cannot fire.
  `aimemory-ingest rebuild-graph` fails outright — the CLI imports `knowledge.structural.rebuild_graph`,
  which does not exist; `structural.py` exports `StructuralIndex`, `structural_plan`, and
  `document_node_id` but no driver function. P7-T03, still open.
- 2026-09-17 — **0 of 81 P8-T04-pilot artifacts carry an evidence quote.**
- 2026-09-17 — **`embeddings` has `UNIQUE(text_hash, model_id)`** while the semantic retrieval SQL
  joins `chunks c ON c.id = e.object_id` — a chunk whose text duplicates another is invisible to
  *semantic* retrieval (keyword search still finds it). Raised by A09 during P9-T01; needs an explicit
  accept-or-fix decision.
- 2026-09-17 — **P8-T04 is 15/144 episodes**, a deliberately scoped pilot, not the full background run.
  P13 (`joblab-de` pilot ingest) not started. P11, P12, P14–P17 not started.
