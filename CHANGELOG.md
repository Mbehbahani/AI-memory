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

### Changed
- 2026-09-14 — Plan §AC rewritten as a decision table (AC-9 now "cap WSL2 at 12 GB", AC-11 added);
  frugal-RAM defaults: `OLLAMA_KEEP_ALIVE=5m`, Neo4j heap 512M / page cache 256M; idle-profile
  expectations added to §Z.
- 2026-09-14 — ADR-0011 offline Ops dashboard (`/ops`) + always-on ingestion worker; agent A16 `ops-dashboard`; task P12-T02; compose `ingestion` service is now a worker (no `ingest` profile).
- 2026-09-14 — `schemas/ontology.yaml`: `SubProject` now carries `stored_as: Project` (19 node types
  stored as 18 Neo4j labels); property-set comments converted to real `node_properties`/
  `relationship_properties` keys with `engine`/`observed_at` required (A02, P1-T01).
