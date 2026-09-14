# Graphiti Compatibility Gate — P4-T04

Owner A05 (run), A00 (write-up) · Date **2026-09-14/15** · Plan §O · [ADR-0002](../docs/adr/ADR-0002-graphiti-gate.md)

> **Status: NO CLEAN VERDICT FROM A LIVE RUN.** The gate was attempted against Bedrock only and
> crashed on the first episode. No Ollama gate run exists. A05 was terminated by an API rate limit
> before it could diagnose or retry. This file records exactly what happened and what can and cannot
> be concluded. **ADR-0009 must not be closed on the strength of this run alone** — but see
> "Arithmetic result" below, which does settle one branch on already-MEASURED evidence.

## What was attempted

`tests/evaluation/local_ai/run_graphiti_gate.py`, `group_id=p4-graphiti-gate-bedrock`,
`llm_provider=bedrock`, `structured_output_mode=anthropic_tool_use`, `max_tokens=2048`,
against the live Neo4j. Raw: `tests/evaluation/local_ai/results/graphiti-gate-bedrock.json`.

`graphiti-core` installed into a throwaway `.venv-graphiti` (so the gate could run at all — it is the
optional `[graphiti]` extra and is deliberately not in the runtime image).

**Schema baseline captured before the run** (this part worked): Graphiti built its own indices in
0.66 s, creating 24 indexes and 18 constraints alongside ours. `nodes_before = 0`.

## What happened

Episode **E01** failed after 0.78 s:

```
TypeError: AsyncMessages.create() got an unexpected keyword argument 'temperature'
  graphiti_core/llm_client/anthropic_client.py, line 397, in generate_response
  graphiti_core/utils/maintenance/node_operations.py, line 275, in _call_extraction_llm
```

`unhandled_exceptions` is recorded as `0` in the result file because the harness caught it per
episode — but this is an **incompatibility between `graphiti-core`'s bundled Anthropic client and the
installed `anthropic` SDK version**, not a model or infrastructure failure.

### What this does and does not prove

- It is **not** evidence about Qwen3, Ollama, our embedding service, or Neo4j. None of them were
  exercised past index creation.
- The traceback passes through `run_graphiti_gate.py:90 wrapped_outer`, a harness monkeypatch. **It is
  therefore not established whether the fault is in `graphiti-core`, in the SDK version pairing, or in
  A05's wrapper.** Declaring a C1 failure from this would be dishonest; declaring a pass is obviously
  impossible.
- Per-criterion status: **C1 unknown · C2 not measured live · C3 not measured · C4 not measured ·
  C5 not measured · C6 partially — indices and constraints were created (24/18), but no vectors were
  written, so the 384-d storage half is unverified.**

## Arithmetic result — the one branch that *is* settled

Graphiti performs **6–10 LLM calls per episode** (plan §O). P4-T02 measured, on real ~800-token
my-vault episodes:

| Provider | median s / single extraction call | implied Graphiti episode (6–10 calls) | C2 threshold (median ≤ 240 s) |
|---|---|---|---|
| `qwen3:4b` (Ollama) | **174.43 s** | **1,047 – 1,744 s** (17–29 min) | **FAIL — 4.4x to 7.3x over** |
| Claude Haiku 4.5 (Bedrock) | **7.74 s** | **46 – 77 s** | **PASS — 3x to 5x under** |

**Graphiti on Ollama fails C2 on arithmetic**, from measurements taken independently of the gate
harness. No gate run is needed to establish this, and it is exactly what plan §A.2 predicted:
*"on this CPU-only laptop Graphiti's 6–10 LLM calls per episode will likely fail the latency
threshold"*. The prediction is now MEASURED rather than expected.

The Bedrock branch is **not** settled: C2 would pass comfortably, but C1/C3/C4/C5/C6 were never
reached because of the SDK fault above.

## Consequence for ADR-0009 and A08

ADR-0002's decision rule is "all six pass → Graphiti; any fail → native engine". For the
configuration the plan actually specified — **Graphiti driven by local Ollama + Qwen3** — C2 fails on
measured arithmetic, so **that configuration is decided: native engine.**

What remains genuinely open is a question the plan never asked, created by ADR-0012: whether
Graphiti driven by *Bedrock* would pass. Resolving it requires fixing the SDK incompatibility and
re-running C1/C3/C4/C5/C6. That is a real choice with a real cost, and it should be made deliberately
rather than by default.

**A00's recommendation to the owner (not yet acted on):** build the **native temporal engine** as
ADR-0002's fallback rule already directs, and do not spend further time on the Graphiti gate. The
reasons are evidence-based, not preference:

1. The native engine is provider-agnostic by construction — it sits behind our `KnowledgeEngine` port
   and calls whichever `LLMProvider` is configured, so it works with **both** providers. A
   Graphiti-on-Bedrock path would work with only one, re-coupling the system to the cloud provider
   that ADR-0012 deliberately kept optional.
2. Native extraction is **2–3 calls per episode**, not 6–10 (plan §M). On Ollama that is ~350–520 s
   per episode — slow, but viable as background work, where Graphiti-on-Ollama at 17–29 min per
   episode is not.
3. ADR-0001 already requires mirroring Graphiti's output into Postgres anyway, so Graphiti was never
   going to save the persistence work.

## Reproduction / what a retry must fix

1. Pin `anthropic` to a version whose `AsyncMessages.create()` accepts the kwargs
   `graphiti_core.llm_client.anthropic_client` passes, or drop the harness monkeypatch at
   `run_graphiti_gate.py:90` and let graphiti call the SDK directly.
2. Re-run the full gate for **both** providers, not just Bedrock, so C1/C3/C4/C5/C6 are scored on
   each.
3. Clean up: `group_id=p4-graphiti-gate-bedrock` and the 24 Graphiti-created indexes / 18 constraints
   are **still present in the live Neo4j** — A05 was terminated before its cleanup step. **A08 and
   A11 must not assume a clean graph.** See "Outstanding cleanup" below.

## Outstanding cleanup (owed)

Graphiti wrote schema objects into the working Neo4j and was not torn down:

- indexes: `application_id`, `concept_id`, `dataset_id`, `decision_id`, `device_id`, `document_id`,
  `entity_fulltext`, `entity_name`, `entity_project`, `entity_valid_to`, `episode_id`,
  `experiment_id`, `finding_id`, `index_343aff4e`, `index_f7700477`, `infra_id`, `organization_id`,
  `person_id`, `project_id`, `repository_id`, `requirement_id`, `source_id`, `task_id`,
  `technology_id`
- constraints: the 18 corresponding `*_id` constraints

Several of these names overlap what A04 created in P5-T02, so **they must be diffed against
`infra/neo4j/schema/constraints.cypher` before anything is dropped** — dropping a constraint our own
migration owns would be worse than leaving Graphiti's behind. `nodes_before = 0` and E01 failed before
writing, so **no data nodes were created**; this is schema residue only.
