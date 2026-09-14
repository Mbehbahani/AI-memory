# Local AI Validation

Phase **P4** · owner A05 · this file currently holds only what has actually been measured.

> **Status: INCOMPLETE.** The P4-T02 measurement campaign (20 runs, realistic ~800-token episodes,
> RAM with the model resident) and the P4-T04 Graphiti gate have **not** run — A05 was terminated by
> an API rate limit twice. What follows is what A00 measured directly. Nothing here is estimated or
> inferred from an agent's unfinished claims.

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

## Still owed (P4)

| Item | Task | Why it matters |
|---|---|---|
| 20-run JSON-schema validity per provider, on ~800-token episodes | P4-T02 | The **stop rule**: first-pass validity < 70 % for the selected provider is a BLOCKER |
| Prompt-eval tok/s at realistic context length | P4-T02 | The 28.5 tok/s above is from a 95-token prompt; prompt eval dominates real episodes and determines whether tiering holds |
| Ollama container RSS with the model **resident** | P4-T02 | Currently **UNKNOWN**; §Z needs it. The 44 MiB figure in `infra-bringup.md` is post-unload |
| Entity/relationship recall against hand-listed expectations | P4-T02 / ADR-0012 | The n=1 evidence above suggests the providers differ most in *quality*, not just speed |
| Graphiti gate C1–C6 and the ADR-0009 verdict | P4-T04 | Decides whether A08 builds on Graphiti or the native engine |
| Cost per episode on Bedrock | ADR-0012 | ESTIMATED < $1 for the whole vault; should be measured once over the benchmark set |
