# ADR-0006 — Tiered ingestion with serial CPU extraction

Status: accepted · Date: 2026-09-13

## Context
Qwen3 4B on a 4-core CPU is ESTIMATED at minutes per episode; the vault alone is ~200 episodes.
Waiting for full extraction before anything is usable would block the project for hours.

## Decision
Tier 0: deterministic registry + structural graph from my-vault (minutes). Tier 1: chunk + embed every
INDEX_CONTENT source (minutes). Tier 2: LLM extraction as a background queue ordered by priority
(`config/source-roots.yaml: priority_paths`; AIOS, Projects, Areas, decision docs, READMEs first;
clippings last). `INGEST_LLM_CONCURRENCY=1`; Ollama limited to `OLLAMA_CPUS` cores. Scans are manual
or scheduled (no file watchers across Windows bind mounts).

## Consequences
+ Queryable after Tier 1; laptop stays usable. − Graph completeness lags; coverage must be reported
honestly (`get_current_state` exposes coverage per project).
