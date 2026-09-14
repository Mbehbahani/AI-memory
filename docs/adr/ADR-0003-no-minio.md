# ADR-0003 — No object store in V0.1; MIRROR policy uses a Postgres blob table

Status: accepted · Date: 2026-09-13

## Context
The principle is "centralize knowledge, not raw files". MIRROR is opt-in for a handful of critical
documents (e.g. `AIOS/me.md`). MinIO would add a service, credentials, and a backup target for a few MB.

## Decision
MIRROR stores raw bytes in `mirror_blobs (version_id, bytes bytea, media_type)`. MinIO is not deployed.
If mirrored volume ever exceeds ~500 MB, revisit with a new ADR.

## Consequences
+ One fewer service and secret. − Postgres backups include blobs (bounded by the opt-in list).
