# Changelog

All notable changes to this project are documented here. Format: Keep a Changelog. Versioning: SemVer.

## [Unreleased]

### Added
- 2026-09-13 — Repository scaffold: directory tree, plan (`docs/architecture/v0.1-plan.md`), ADR-0001…0008,
  agent definitions (`.claude/agents/`), task plan, contracts (`schemas/`), config templates, Compose
  topology draft, reporting skeleton. No services built or started.
- 2026-09-14 — ADR-0010 ongoing evaluation protocol (metrics snapshots, human review sampling, frozen
  benchmark set, model-upgrade rule) and plan §AF; task P14-T05.

### Changed
- 2026-09-14 — Plan §AC rewritten as a decision table (AC-9 now "cap WSL2 at 12 GB", AC-11 added);
  frugal-RAM defaults: `OLLAMA_KEEP_ALIVE=5m`, Neo4j heap 512M / page cache 256M; idle-profile
  expectations added to §Z.
- 2026-09-14 — ADR-0011 offline Ops dashboard (`/ops`) + always-on ingestion worker; agent A16 `ops-dashboard`; task P12-T02; compose `ingestion` service is now a worker (no `ingest` profile).
