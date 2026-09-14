# Core Infrastructure Bring-up — P3

Tasks **P3-T01, P3-T02, P3-T03** · Date **2026-09-14** · All numbers MEASURED on this host unless labelled otherwise.

> **Provenance note.** P3-T01/T02 were started by A03 and P3-T03 by A06, but all four background
> agents were terminated mid-run by a session/API limit before they could verify or report. The
> containers and the P3-T03 application code they had already produced were left on disk. A00 (the
> orchestrator) then performed every verification recorded below directly and wrote this report.
> Nothing here is inherited from an agent's unfinished claims; every line was re-measured.

## P3-T01 — PostgreSQL + Neo4j

`docker compose up -d postgres neo4j` (base + dev override).

| Check | Command | Result |
|---|---|---|
| postgres health | `docker compose ps` | `Up (healthy)` |
| neo4j health | `docker compose ps` | `Up (healthy)` |
| extensions | `psql -tAc "select extname from pg_extension order by 1"` | `pg_trgm`, `pgcrypto`, `plpgsql`, `vector` — all three required extensions present, created by `infra/postgres/init/01-extensions.sql` |
| APOC | `SHOW PROCEDURES YIELD name WHERE name STARTS WITH 'apoc' RETURN count(*)` | **190** procedures |
| Neo4j memory in effect | container env | `heap=512M pagecache=256M` — the frugal defaults of plan §Z |

## P3-T02 — Ollama + qwen3:4b

| Check | Result |
|---|---|
| `ollama list` | `qwen3:4b`, id `359d7dd4bcda`, **2.5 GB** |
| health | `Up (healthy)` |

**Schema-constrained decoding smoke test** (`POST /api/chat`, `think=false`, `temperature=0`,
`num_ctx=8192`, `format=` a two-property JSON Schema with `additionalProperties:false`):

```
prompt: "The JobLab Lakehouse project is currently active. Return its name and status as JSON."
response: {"project": "JobLab Lakehouse", "status": "active"}
valid JSON: yes
```

| Metric | Value | Label |
|---|---|---|
| total wall clock (cold, includes model load) | 9.98 s | MEASURED |
| model load | 6.95 s | MEASURED |
| prompt eval | 28 tokens / 0.856 s = **32.7 tok/s** | MEASURED |
| generation | 20 tokens / 2.168 s = **9.2 tok/s** | MEASURED |

Generation speed lands inside the plan's ESTIMATED 8–12 tok/s (§Z). Prompt eval at 32.7 tok/s is
**below** the ESTIMATED 40–80 tok/s band — this is a single 28-token sample, far too small to
generalise; A05's P4-T02 must measure it properly on ~800-token episodes, where prompt eval dominates
the per-episode cost. Do not plan capacity from this number.

`B10` (qwen3:4b with `think=false` and Ollama `format=` JSON-schema decoding is available) is
**confirmed** by this test.

## P3-T03 — Embedding service (MiniLM)

Built and started with `docker compose up -d --build embedding-service` (173 s including build,
torch layers cached from P2-T02).

| Check | Result |
|---|---|
| health | `Up (healthy)`, published on `127.0.0.1:8010` only |
| `/health` payload | model loaded, `sentence-transformers/all-MiniLM-L6-v2`, dimension 384, revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, normalized, max_seq 256, torch threads 2, offline true, load 0.227 s |
| `/embed` | 384-d vectors, L2 norm **1.000000**, identical input → **byte-identical** output (determinism holds, so the `(text_hash, model_id)` reuse key is sound) |
| `/v1/embeddings` | OpenAI shape verified: `object=list`, per-item `object=embedding` + `index`, 384-d, `usage.prompt_tokens` present. This is the route `graphiti-core`'s `OpenAIEmbedder` needs for the P4 gate. |

**Offline acceptance (the P3-T03 criterion)** — `docker run --network none`:

```
HEALTH offline: {'status': 'ok', 'model_loaded': True, 'dimension': 384, 'offline': True}
dim: 384 norm: 1.0
NETWORK: unreachable -> gaierror
```

The model loads and serves from the baked image with no network, and the network is provably absent.
`B16` (no cloud calls at runtime) is confirmed for this service.

## Port bindings — ADR-0007 acceptance

`Get-NetTCPConnection -State Listen`, host level:

```
LocalAddress LocalPort
------------ ---------
127.0.0.1         5432
127.0.0.1         7474
127.0.0.1         7687
127.0.0.1        11434
```

plus `127.0.0.1:8010` for the embedding service (`docker compose ps`). **Nothing is bound to
`0.0.0.0`.** ADR-0007 holds for every service brought up so far. 8000 / 8020 / 5005 are not yet
published because memory-api, mcp-server and NeoDash are not up (P10/P11/P12).

## Measured resource use

`docker stats --no-stream`, idle, host VM ceiling 15.46 GiB:

| Container | RSS idle | Label |
|---|---|---|
| ollama (model **unloaded**) | 44.3 MiB | MEASURED |
| postgres | 50.3 MiB | MEASURED |
| neo4j | 539.1 MiB | MEASURED |
| embedding-service (after a 256-text batch) | 496.3 MiB | MEASURED |
| **total, four services idle** | **≈ 1.11 GiB** | MEASURED |

This is well under the plan's ESTIMATED 3–4 GB idle figure (§Z) — but that estimate covers the full
eight-container stack, and ingestion/memory-api/mcp-server/NeoDash are not running yet. The
`OLLAMA_KEEP_ALIVE=5m` unload behaviour is confirmed by the 44 MiB ollama figure taken well after the
smoke test: the model does drop out of memory.

RAM with the model **loaded** was not captured (the 44 MiB reading is post-unload) — **UNKNOWN**,
to be measured by A05 in P4-T02 during the sustained extraction run.

## MiniLM throughput (feeds P4-T03)

| Metric | Value | Label |
|---|---|---|
| batch of 256 texts of ~200 tokens | 8.33 s → **30.7 texts/s** (server-side 8.03 s) | MEASURED |
| single-text latency, n=20 | p50 **14.2 ms**, p95 **21.1 ms** (min 11.9, max 23.2) | MEASURED |
| container RSS after the batch | 496.3 MiB | MEASURED |

At 30.7 texts/s the vault's ESTIMATED ~2,000 chunks embed in roughly **65 seconds**, consistent with
the plan's "< 2 min" ESTIMATED figure for Tier 1 (§Z). Query-time embedding at ~14 ms p50 is
negligible inside the retrieval budget.

## Status

- **P3-T01 — done.** Both databases healthy, extensions and APOC verified, loopback-only.
- **P3-T02 — done.** `qwen3:4b` present and producing schema-valid JSON; capacity numbers deferred to P4-T02.
- **P3-T03 — done.** Service healthy, 384-d, normalized, deterministic, OpenAI-compatible route working, offline proven.

## Carried forward

- The embedding provider client `packages/aimemory/providers/embedding/http.py` (A06) is written but
  **has no test run against it** — A06 died before writing `tests/unit/test_embedding_provider.py` and
  `tests/integration/test_embedding_service.py`. Those tests are still owed.
- Ollama RAM with the model resident: UNKNOWN, owed by P4-T02.
- Prompt-eval throughput on realistic episode sizes: owed by P4-T02.
