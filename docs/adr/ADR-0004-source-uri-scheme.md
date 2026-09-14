# ADR-0004 — Logical source URIs instead of absolute paths

Status: accepted · Date: 2026-09-13

## Context
Sources live on one laptop today, but the model must not assume that forever; absolute Windows paths
are fragile and leak into provenance.

## Decision
Every source has a `source_uri`: `vault://<root-label>/<relative>`,
`localfs://<device_id>/<root-label>/<relative>`, `git://<repo-label>/<relative>` (+ commit in version
metadata). Host paths appear only in `.env` and Compose mounts; container paths only in
`config/source-roots.yaml`. `device_id = local-development-machine` in V0.1. Multi-device connectors
are out of scope.

## Consequences
+ Provenance is portable; roots can move. − A resolver maps a URI to a mounted path (the Gateway
returns the current mapping only if the root is mounted and never returns file bytes).
