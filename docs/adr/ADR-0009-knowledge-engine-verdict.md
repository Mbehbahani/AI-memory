# ADR-0009 — Knowledge engine verdict: the native temporal engine, not Graphiti

- **Status:** accepted
- **Date:** 2026-09-15
- **Deciders:** orchestrator A00, on the owner's instruction to select the best approach
- **Decides:** the gate opened by [ADR-0002](ADR-0002-graphiti-gate.md)
- **Related:** [ADR-0001](ADR-0001-postgres-system-of-record.md),
  [ADR-0012](ADR-0012-dual-llm-provider-evaluation.md), [ADR-0014](ADR-0014-extraction-provider-and-model-consistency.md)
- **Evidence:** [`reports/graphiti-gate.md`](../../reports/graphiti-gate.md),
  [`reports/local-ai-validation.md`](../../reports/local-ai-validation.md)

## Context

ADR-0002 made Graphiti conditional on a measured gate (plan §O, criteria C1–C6) with an automatic
fallback to a native temporal engine. The gate ran on 2026-09-14 and **did not complete**: it crashed
on the first episode with `TypeError: AsyncMessages.create() got an unexpected keyword argument
'temperature'` inside `graphiti_core/llm_client/anthropic_client.py` — an incompatibility between
`graphiti-core`'s bundled Anthropic client and the installed SDK. Only the Bedrock branch was
attempted. C1, C3, C4 and C5 were never measured.

A crashed harness is not evidence about the engine, and this ADR does not treat it as such. The
decision rests instead on measurements taken **independently of the gate**, in the P4-T02 campaign
(n=20 real my-vault episodes per provider, frozen schemas):

Graphiti performs **6–10 LLM calls per episode** (§O). Measured median seconds per single extraction
call, and the resulting per-episode cost against C2's `median ≤ 240 s`:

| Provider | median s / call | Graphiti episode (6–10 calls) | C2 |
|---|---|---|---|
| `qwen3:4b` (Ollama) | **174.43 s** | **1,047 – 1,744 s** (17–29 min) | **FAIL — 4.4× to 7.3× over** |
| Claude Haiku 4.5 (Bedrock) | **7.74 s** | 46 – 77 s | would pass |

For the configuration the approved plan actually specified — Graphiti driven by local Ollama and
Qwen3 — **C2 fails on arithmetic**, with no gate run required. Plan §A.2 predicted precisely this
outcome; it is now MEASURED rather than expected. ADR-0002's rule is "any criterion fails → native
engine", and a criterion has failed.

ADR-0012 subsequently created a question the plan never posed: Graphiti driven by *Bedrock* would
clear C2 comfortably. That branch remains genuinely unmeasured on C1/C3/C4/C5/C6.

## Decision

**Build the native temporal engine. Do not pursue Graphiti further in V0.1**, including the
unmeasured Bedrock branch.

Three reasons, in order of weight:

1. **Provider independence.** The native engine sits behind our `KnowledgeEngine` port and calls
   whichever `LLMProvider` is configured. It therefore works with **both** providers built under
   ADR-0012. Graphiti-on-Bedrock would work with exactly one, re-coupling the knowledge layer to the
   cloud provider that ADR-0012 deliberately kept optional — and would leave the offline mode with no
   extraction engine at all.
2. **Call budget.** Native extraction is **2–3 calls per episode** (§M) against Graphiti's 6–10. On
   Ollama that is ESTIMATED 350–520 s per episode — slow, but viable as background work, where
   Graphiti-on-Ollama at 17–29 minutes per episode is not. This keeps the local path *usable* rather
   than nominal.
3. **No persistence saving.** ADR-0001 requires mirroring any engine's output into Postgres so that
   Postgres stays the system of record and Neo4j stays rebuildable. Graphiti was therefore never
   going to save the persistence work that dominates this phase.

The gate's SDK incompatibility is **not** cited as a reason. It is a fixable packaging problem, and
using it as justification would be dishonest about why the decision was taken.

## Consequences

- **A08 builds `packages/aimemory/knowledge/native_engine/`** to the `KnowledgeEngine` port
  (`process_episode`, `invalidate`), using our prompts, the frozen `schemas/extraction/*.json`, and
  `get_provider()` from ADR-0012. `graphiti_engine/` remains an empty package as a documented
  extension point; it is not deleted, so a future version can revisit the question cheaply.
- **`graphiti-core` stays an optional extra and enters no runtime image.** It is not a dependency of
  V0.1.
- **The gate's residue must be cleaned up before A08 starts.** Graphiti created 24 indexes and 18
  constraints in the live Neo4j and its teardown never ran. `nodes_before = 0` and E01 failed before
  writing, so **no data nodes exist** — this is schema residue only. Several names collide with the
  constraints A04 created in P5-T02, so they **must be diffed against
  `infra/neo4j/schema/constraints.cypher` before anything is dropped**; dropping a constraint our own
  migration owns would be a worse outcome than leaving Graphiti's behind.
- **ADR-0002 is satisfied, not superseded.** Its mechanism — measure, then fall back automatically —
  worked exactly as designed, and produced a decision without a stop.
- **Reopening condition.** If V0.1 ever moves to hardware where a single extraction call is under
  ~25 s locally (6–10 calls inside C2's 240 s), the Graphiti question becomes live again and this ADR
  should be revisited with a superseding record rather than amended.

## Rejected alternatives

- **Fix the SDK pairing and re-run the full gate.** Rejected on cost/benefit: it would settle only the
  Bedrock branch, and reason 1 above rules that branch out regardless of how it scored.
- **Adopt Graphiti for Bedrock and the native engine for Ollama.** Rejected outright — two engines
  producing the graph is exactly the model-mismatch failure ADR-0014 exists to prevent, and it would
  double the surface that every temporal and provenance test must cover.
- **Declare C1 failed and cite the crash.** Rejected as dishonest; see above.
