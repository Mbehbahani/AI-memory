# ADR-0011 — Built-in offline Ops dashboard and an always-on ingestion worker

Status: accepted · Date: 2026-09-14 · Deciders: owner (Mohammad), A00

## Context
The owner operates the system occasionally (run updates, check evaluation, review samples). A CLI-only
operator surface will not be used weekly. Health metrics and evaluation data live in Postgres, which
NeoDash cannot read. Grafana/Prometheus or a separate Streamlit app would add containers and RAM
against the frugal-RAM constraint.

## Decision
1. memory-api serves an **Ops page** at `/ops` (server-rendered HTML + HTMX, inline SVG charts, no
   build step, loopback only, no auth in V0.1). Sections: Health, Runs, Coverage, Quality (ADR-0010
   metrics and trends), Review queue (accept/wrong/partial form → `extraction_reviews`), Attention list.
2. The ingestion service becomes an always-on **worker** (`aimemory-ingest worker`, `restart:
   unless-stopped`, idle ≈ 0.2 GB ESTIMATED) that polls a `run_requests` table (root, tier, requested_by,
   status, progress). The Ops page and the CLI both create requests; the worker executes them serially,
   updates progress, and writes `metrics_snapshots` and a `service_stats` snapshot (container RAM from
   `docker stats` is **not** available inside containers, so the worker records its own process RSS and
   Ollama's `/api/ps` model-loaded state; host-level RAM is shown by `scripts/doctor` instead — labelled).
3. Containers never receive the Docker socket. Actions available from the page are limited to:
   run scan, retry failed, run gold-set eval, run benchmark, record review verdicts. Anything else
   (backup, restart, model switch) remains a documented script/CLI action.
4. NeoDash stays the graph-exploration tool; the Ops page links to it.

## Consequences
+ One place to run updates and judge quality; the ADR-0010 routine takes minutes in a browser.
− One Sonnet agent (A16), task P12-T02, the worker process (~0.2 GB idle), and two small tables.
− Remote access is explicitly out of scope (would need auth/TLS; V0.2, tied to the multi-device boundary).
