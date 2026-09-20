# P16-T01 — Clean-deployment verification

**Date:** 2026-09-18 · **Verified by:** A00 (orchestrator) · **Result: PASS**

A second, isolated copy of the stack was built and started from the repository alone, to answer one
question: does `docker compose up -d` work for someone who has *not* watched this system being
built? Everything here is MEASURED.

This mattered because the system had been kept working by repeated live fixes. Three defects found
earlier in the build were of exactly the kind that only appear on a machine nobody has hand-patched:
the ingestion image never copied the Alembic files (`docker compose up -d` died at `migrate`);
`memory-api` shipped a placeholder command that could never import; neither image installed the
`bedrock` extra, so `boto3` was simply absent. A fourth was found the day before this run — the
`migrate` service has its **own** `build:` block using the same Dockerfile as `ingestion`, so
rebuilding one leaves the other stale, and the database sat at a revision its own image could not
locate.

## How it was run

Deliberately **not** a copy of the working `.env`. The environment was generated from
`.env.example` — what a fresh user actually gets — changing only:

* the three `change-me` passwords,
* `HOST_VAULT_ROOT` / `HOST_PILOT_ROOT` (the only two variables compose declares mandatory),
* host ports `8100` / `8120`, to avoid colliding with the running stack.

`HOST_AWS_DIR` was left **unset**, so the run also tests the documented promise that the stack comes
up with zero AWS configuration.

```
docker compose -p ai-memory-verify -f docker-compose.yml --env-file <fresh env> up -d
```

Base compose only, with no dev override — again, what a fresh user gets. Compose resolved the
project as `name: ai-memory-verify` and published only `8100` and `8120`, confirmed before anything
was started.

## Result — every service healthy from a cold start

| Service | Status |
|---|---|
| postgres | Up (healthy) |
| neo4j | Up (healthy) |
| embedding-service | Up (healthy) |
| migrate | Exited (0) — one-shot, correct |
| memory-api | Up (healthy) |
| mcp-server | Up (healthy) |
| ingestion | Up |

* `GET :8100/health` → `{"status":"ok"}` with postgres, neo4j and embedding all reachable
  (4 ms / 16 ms / 3 ms).
* `GET :8120/health` → 200.
* Fresh database migrated to `0003_narrow_functional_index`, **29 tables**.
* Corpus **0 sources, 0 facts** — correct for a new install, and proof the volumes were genuinely
  empty rather than shared.
* A real MCP client connected (`ai-memory 1.30.0`), listed **12 tools** and both resources, and
  `memory://projects` returned `"count": 0`.

**No defect was found.** The four historical deployment defects listed above are all genuinely fixed:
this run exercised every one of them from cold, including the stale-image bug found the previous day.

## Isolation

The live stack was untouched throughout. Volumes are namespaced by project and never shared:

```
ai-memory_pg_data            ai-memory-verify_pg_data
ai-memory_neo4j_data         ai-memory-verify_neo4j_data
ai-memory_ingestion_state    ai-memory-verify_ingestion_state
```

Live corpus before and after the entire exercise: **241 sources, 1,244 facts, 614 entities** —
identical. All seven live services remained healthy.

## Teardown, and what is left behind

`docker compose -p ai-memory-verify ... down` removed the verification containers and network.

The verification **volumes were deliberately left in place.** `CLAUDE.md` says plainly: *never
`docker compose down -v`, never delete volumes.* That rule exists to protect the corpus, and a
verification project is arguably outside its intent — but the rule is written without exception, and
the cost of honouring it here is a few GB of disk rather than a risk to data. Removing them is a
one-line, explicitly scoped command for the owner:

```
docker compose -p ai-memory-verify -f docker-compose.yml --env-file <env> down -v
```

The four volumes are `ai-memory-verify_{pg_data,neo4j_data,neo4j_import,neo4j_logs,ingestion_state}`.
They contain no user data — the database was verified empty (0 sources, 0 facts) before teardown.

## Limitation of this verification

It ran on the machine that built the system, so it proves the *repository* is self-sufficient: images
build from the Dockerfiles, migrations apply to an empty database, services reach health, and the
API and MCP surfaces answer. It does **not** prove a different machine or OS would behave the same —
it reuses this host's Docker daemon, its image layer cache and its pulled base images. P16-T02
(a fresh-machine walkthrough following only the written documentation) remains unperformed.
