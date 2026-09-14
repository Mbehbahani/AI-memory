# Local AI Validation

Phase **P4** · owner A05 · this file currently holds only what has actually been measured.

> **Status: P4-T02 COMPLETE, P4-T04 INCOMPLETE.** The measurement campaign ran to completion for both
> providers (n=20 each). The Graphiti gate did **not** produce a verdict — see `reports/graphiti-gate.md`.
> A05 gathered the P4-T02 data but was terminated by an API rate limit before writing it up; A00 wrote
> the analysis below directly from the raw result files, which are committed.

---

## MiniLM embeddings (P3-T03 / P4-T03) — MEASURED 2026-09-14 by A00

Service: `ai-memory-embedding-service`, `sentence-transformers/all-MiniLM-L6-v2`, HF revision
`1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, 384-d, L2-normalized, CPU, `TORCH_NUM_THREADS=2`.

| Metric | Value | Label |
|---|---|---|
| model load at startup | 0.227 s | MEASURED |
| batch of 256 texts of ~200 tokens | 8.33 s → **30.7 texts/s** (server-side 8.03 s) | MEASURED |
| single-text latency, n=20 | p50 **14.2 ms**, p95 **21.1 ms** (min 11.9, max 23.2) | MEASURED |
| container RSS after the batch | 496.3 MiB | MEASURED |
| determinism | identical text → byte-identical vector | MEASURED |
| offline | loads and serves under `docker run --network none`; DNS provably dead | MEASURED |

At 30.7 texts/s the vault's ESTIMATED ~2,000 chunks embed in roughly **65 s**, consistent with the
plan's "< 2 min" for Tier 1 (§Z). Query-time embedding at ~14 ms p50 is negligible in the retrieval
budget. **P4-T03 is satisfied.**

---

## Extraction providers — head-to-head, n=1 (ADR-0012)

> **This is a single-episode smoke comparison, not the P4-T02 campaign.** One episode, one run per
> provider, no repetition. It is enough to prove both providers work end to end against the frozen
> schema and to show the order of magnitude; it is **not** enough to state a validity rate, a median,
> or a quality score. Do not generalise from it. P4-T02 must still run 20 episodes per provider on
> the frozen benchmark set.

**Input (identical for both):** a 4-line decision record naming JobLab Lakehouse, DuckDB, Postgres,
Supabase, row-level security, Mohammad and JobLabScraperAPI.
**Schema (identical for both):** `schemas/extraction/episode_extraction.schema.json`, verbatim,
required `['doc_kind', 'summary', 'entities', 'artifacts']`.

| Metric | `qwen3:4b` (Ollama, local) | Claude Haiku 4.5 (Bedrock) | Label |
|---|---|---|---|
| wall clock | **65.97 s** | **6.59 s** | MEASURED |
| valid on first attempt | yes | yes | MEASURED |
| attempts used | 1 | 1 | MEASURED |
| model load | 6.91 s (cold) | n/a (managed) | MEASURED |
| prompt tokens | 95 @ 28.5 tok/s | 1,410 | MEASURED |
| generated tokens | 402 @ 7.2 tok/s | 954 | MEASURED |
| structured-output mechanism | Ollama `format=<schema>` → bare JSON | forced tool use (`stop_reason: tool_use`) | MEASURED |

**Roughly 10× wall-clock difference on this episode.** The token counts are not comparable
(the Bedrock path sends the schema as a tool definition, inflating input tokens), and Bedrock reports
no prompt/generation time split, so its tok/s cannot be compared to Ollama's — see
`BedrockCallTrace.gen_tok_s`, which is deliberately documented as an under-estimate.

### Extraction quality on this episode (qualitative, n=1)

Both returned schema-valid output. The content differed in ways that matter for the graph:

| | `qwen3:4b` | Haiku 4.5 |
|---|---|---|
| `JobLab Lakehouse` entity type | **`Dataset`** — wrong; the registry seeds it as a `Project`, so this would create a duplicate, mistyped node that entity resolution has to repair | `Project` — correct |
| Postgres alias | missed — emitted `Postgres` only | captured `PostgreSQL` with `aliases: ["Postgres"]` |
| `summary` field | echoed the heading ("Architecture decision: storage layer") rather than summarising | a real summary of the decision and its rationale |
| text integrity | emitted `"JobLab Lake:house"` — a corrupted token inside a description | clean |

The mistyped `Project`→`Dataset` is the most consequential: it is precisely the failure mode that
risk **R2** (Qwen3 4B reliability) anticipated, and the one that deterministic-first entity
resolution (P8-T03) exists to catch. One episode does not establish a rate — but it does mean
P4-T02 must score **entity-type correctness**, not only schema validity, for both providers.

### Reproduction

```bash
# Bedrock  (AWS_PROFILE=mohabehb, us-east-1)
python -c "from aimemory.providers.llm import BedrockProvider; ..."   # see the P4-T02 harness
# Ollama
OLLAMA_URL=http://127.0.0.1:11434 python -c "from aimemory.providers.llm import OllamaProvider; ..."
```

Both were driven through the same `complete_json_traced` interface so the attempt counts and traces
are directly comparable.

---

## P4-T02 — measurement campaign, both providers, n=20 (MEASURED 2026-09-14/15)

Harness `tests/evaluation/local_ai/run_benchmark.py`; raw results
`tests/evaluation/local_ai/results/bench-{ollama,bedrock}-p4t02.json`. **20 real my-vault episodes,
identical for both providers**, frozen `episode_extraction.schema.json`; plus 10 relationship
episodes against `relationship_extraction.schema.json`. Validator: `jsonschema 4.26.0`.
Expected entities and relationships were hand-listed per episode *before* the runs
(`benchmark_expectations.py`).

| Metric | `qwen3:4b` (Ollama, local) | Claude Haiku 4.5 (Bedrock) | Label |
|---|---|---|---|
| **first-pass JSON validity** | **95.0 %** (19/20) | **95.0 %** (19/20) | MEASURED |
| validity after retries | 95.0 % (19/20) | **100.0 %** (20/20) | MEASURED |
| failed outright | 1 (truncated at `num_predict=1024`) | 0 | MEASURED |
| transport failures | 0 | 0 | MEASURED |
| **median s / episode** | **174.43** | **7.74** | MEASURED |
| p90 s / episode | 219.87 | 9.46 | MEASURED |
| min / max s | 127.48 / 895.06 | 5.02 / 10.71 | MEASURED |
| **mean entity recall** | **68.9 %** | **68.9 %** | MEASURED |
| median entity recall | 71.4 % | 69.0 % | MEASURED |
| **entity-type correctness** | **85.7 %** (19 type errors) | **91.8 %** (11 type errors) | MEASURED |
| **mean relationship recall** | **40.2 %** | **61.2 %** | MEASURED |
| relationship first-pass validity | 5/10 | 9/10 | MEASURED |
| relationship final validity | 7/10 | 10/10 | MEASURED |
| median s / relationship call | 347.12 | 5.08 | MEASURED |

**Both providers clear the 70 % first-pass-validity stop rule** (95 % each). Schema validity is
*not* the discriminator — it is a tie. The differences are in **relationship extraction**
(40.2 % vs 61.2 %) and **speed** (22.5x on episodes, 68x on relationship calls).

### The stop-rule-adjacent finding: relationship recall

Plan §O criterion **C3 requires >= 50 % of expected relationships**. `qwen3:4b` scores **40.2 %** —
**below that bar**. Haiku scores 61.2 % and clears it. Entity recall is identical at 68.9 % for both
(C3's >= 60 % entity bar is met by both). So on the plan's own quality thresholds, the local model
passes on entities and **fails on relationships**, which are exactly what makes the graph a graph
rather than a list of nouns.

### Qwen3 4B throughput degrades with context — the plan's estimate does not hold

Size sweep on one episode at three lengths (MEASURED):

| Prompt size | prompt tokens | prompt tok/s | gen tok/s | wall |
|---|---|---|---|---|
| 800 chars | 460 | 32.39 | **6.57** | 67.8 s |
| 3,200 chars | 1,114 | 30.35 | **4.26** | 214.3 s |
| 8,000 chars | 2,064 | 19.83 | **3.31** | 376.5 s |

At realistic episode length (median prompt **1,077 tokens**) the campaign measured
**31.87 prompt tok/s and 5.37 generation tok/s**. Plan §Z ESTIMATED "8-12 tok/s gen" — the real
figure at working context is **3.3-6.6 tok/s**, roughly **half**. The earlier 9.2 tok/s figure came
from a 20-token generation and does not generalise, as flagged when it was recorded.

### Ollama RAM with the model resident — was UNKNOWN, now MEASURED

`docker stats` sampled 300 times across the run
(`tests/evaluation/local_ai/results/docker-stats.log`):

| | value |
|---|---|
| min | 3.70 GiB |
| median | **8.01 GiB** |
| p90 | 10.82 GiB |
| **max** | **11.37 GiB** |

Plan §Z ESTIMATED "Ollama + qwen3:4b loaded (8k ctx): 4-5.5 GB". The MEASURED peak is **11.37 GiB,
about 2x the estimate.** With the VM ceiling at 15.46 GiB and the other three services drawing
~1.2 GiB, extraction runs leave roughly 3 GiB of headroom at p90.

> **This retroactively validates the owner's AC-9 decision.** AC-9 (cap WSL2 at 12 GB) was declined.
> Had it been applied, ollama alone at p90 (10.82 GiB) plus the rest of the stack would have exceeded
> the cap and extraction would have OOM-killed. **Do not apply a 12 GB cap while local extraction is
> in use.** ESTIMATED minimum safe cap if one is ever wanted: 14 GB.

### Bedrock cost — MEASURED, and higher than the earlier estimate

| | value |
|---|---|
| tokens in / out (20 episodes + 10 relationship calls) | 54,088 / 22,366 |
| cost, first call per episode | $0.16592 |
| **cost, all calls** | **$0.23797** |
| **per episode (first call)** | **$0.008296** |
| price basis | $1.00 / MTok in, $5.00 / MTok out |

Extrapolated to the vault's ESTIMATED ~200 episodes: **~$1.66** (ESTIMATED, from the MEASURED
per-episode cost), not the "< $1" figure estimated in ADR-0012 before the campaign. Still
negligible, but the ADR-0012 estimate is corrected here rather than left standing.

### Type errors are systematic, not random

Both providers confuse the vault's PARA folders (`00 Inbox`, `01 Projects`, `06 Outputs`) for
structural entities — qwen3 calls them `Repository`, Haiku calls them `InfrastructureComponent`;
both should be `Document|Concept`. Both mistype `Personal Harness` as `Technology`. qwen3 additionally
mistyped a person (`Mbehbahani` -> `Organization`, `Mohammad` -> `InfrastructureComponent`) and
repeatedly typed tools as `Project` (`GitHub Copilot`, `Claude Code`, `OpenClaw`).

Two consequences for A08 (P8-T03): deterministic-first entity resolution must (a) seed the PARA
folder names as known `Document`/`Concept` entities so neither model gets a vote on them, and
(b) treat a `Person` misclassification as a repair case, since a person typed as an `Organization`
corrupts ownership edges.

---

## Still owed (P4)

| Item | Task | Why it matters |
|---|---|---|
| Graphiti gate C1–C6 and the ADR-0009 verdict | P4-T04 | Decides whether A08 builds on Graphiti or the native engine |
