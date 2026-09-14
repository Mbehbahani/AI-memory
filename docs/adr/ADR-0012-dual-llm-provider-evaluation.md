# ADR-0012 — Two extraction providers are built and compared; neither is privileged

- **Status:** accepted
- **Date:** 2026-09-14
- **Deciders:** owner (Mohammad), orchestrator A00
- **Supersedes:** nothing. **Amends:** assumption B16 and plan §T for the extraction step only.
- **Related:** [ADR-0002](ADR-0002-graphiti-gate.md) (measured gate with automatic fallback),
  [ADR-0010](ADR-0010-evaluation-protocol.md) (permanent benchmark set and model-upgrade rule),
  [ADR-0007](ADR-0007-loopback-exposure.md), [ADR-0009](ADR-0009-knowledge-engine-verdict.md) (pending).

## Context

The approved V0.1 plan specifies a single extraction engine: `qwen3:4b` served by a local Ollama
container. Assumption **B16** states "No cloud calls at runtime"; plan **§T** and decision **AC-8**
state that nothing leaves the machine; the frozen `LLMProvider` port docstring said in as many words
that the provider "never falls back to a cloud model".

That premise was built on an expectation about effort and cost that the measurements did not bear out,
and on a risk the plan already flagged:

- **The local engine is already deployed and working** (P3-T02, commit `f3293f0`). `qwen3:4b` is
  pulled (2.5 GB, digest `359d7dd4bcda`), healthy, and Ollama's `format=<json schema>` constrained
  decoding returns raw valid JSON. MEASURED: 9.2 tok/s generation, 6.95 s model load. There is no
  remaining deployment effort that switching away would avoid.
- **Extraction quality on a 4B model is risk R2**, the plan's own second-highest risk, and it has not
  yet been measured on realistic episodes (P4-T02 did not run — the agent was terminated by an API
  rate limit).
- **Cost is not the discriminator.** MEASURED: Claude Haiku 4.5 is reachable in the owner's AWS
  account (`780822965578`, profile `mohabehb`, `AdministratorAccess`) through the US inference
  profile `us.anthropic.claude-haiku-4-5-20251001-v1:0` in `us-east-1`, ~1.6 s round trip. ESTIMATED
  cost to extract the whole vault (~200 episodes) is **well under $1**.

On 2026-09-14 the owner, told explicitly that using Bedrock means every vault note — including
personal notes, CVs and job applications — is sent to AWS, decided: *"if possible use both and
compare the performance in evaluation, but it does not matter to consider which one as the reference."*

## Decision

1. **Two `LLMProvider` implementations are built and maintained**: the existing `OllamaProvider`
   (`qwen3:4b`, local, offline) and a new `BedrockProvider` (Claude Haiku 4.5 via the US inference
   profile). Both sit behind the frozen port; no consumer of the port changes.
2. **Neither is the reference implementation.** `LLM_PROVIDER` selects one at runtime. The default
   stays `ollama` so that a checkout with no AWS credentials still runs fully offline, but this is a
   *default*, not a statement of preference, and the evaluation treats the two symmetrically.
3. **Both are measured on the same frozen benchmark set** defined by ADR-0010
   (`tests/evaluation/benchmark/`: the 10 real my-vault episodes plus the Architecture-A/B
   supersession fixture, with hand-listed expected entities and relationships). P4-T02 reports
   JSON-schema validity, entity/relationship recall, seconds per episode and RAM for **each**
   provider, side by side, in `reports/local-ai-validation.md` and
   `reports/benchmark-<model>-<ts>.md`.
4. **Provenance distinguishes them.** `extraction_models` gets a row per provider
   (`provider` = `ollama` | `bedrock`), and every fact and artifact stamps `extraction_model_id`. A
   graph built partly by one model and partly by the other remains explainable — this is the same
   mechanism ADR-0010 specified for a model upgrade, reused unchanged.
5. **Strict JSON is obtained differently per provider and this is not optional.** Ollama uses
   `format=<json schema>` and returns a bare JSON object. Bedrock has no equivalent; MEASURED, a
   plain prompt returns JSON wrapped in a markdown fence. The `BedrockProvider` therefore uses
   **forced tool use** — the extraction schema is passed as a tool `input_schema` with
   `tool_choice: {"type": "tool", ...}` — so schema conformance is guaranteed by the API rather than
   by parsing prose. The frozen schemas in `schemas/extraction/` are used verbatim by both paths.

## Consequences

### What this costs

- **B16 no longer holds unconditionally.** When `LLM_PROVIDER=bedrock`, source text leaves the
  machine and is sent to AWS Bedrock in `us-east-1`. This is a deliberate, owner-approved amendment,
  scoped to the extraction step only. Everything else stays local: embeddings (MiniLM, baked and
  offline-proven), storage, retrieval, the graph, and the MCP surface.
- **§T and the threat model must be rewritten**, not patched. A13 (P15) must treat "vault content is
  transmitted to a third party under one configuration" as a first-class item: which roots, which
  policies, what a `secret_suspected` file does (it must **never** be sent — the secret detector runs
  before extraction and metadata-only sources have no text to send), and what the audit trail shows.
- **AC-8 ("nothing leaves the machine") is now conditional** on the selected provider and must be
  restated that way in `README.md` and `docs/operations/`.
- A credential path into the stack now exists. The `ingestion` container needs AWS credentials to use
  Bedrock. They must come from the standard chain (mounted `~/.aws` read-only, or environment), must
  never be baked into an image, and must never be logged. `scripts/doctor.ps1 -Security` gains a check.

### What it buys

- R2 (Qwen3 4B reliability) stops being a single point of failure: if local extraction quality is
  poor, there is a measured alternative rather than a blocked build.
- The comparison is evidence the plan wanted anyway. ADR-0010's model-upgrade rule exists precisely to
  compare two models on identical input; this ADR runs that machinery once, early, with a stronger
  contender than `qwen3:8b`.

### Rejected alternatives

- **Switch to Bedrock outright and drop Ollama.** Rejected: it discards working, verified, committed
  infrastructure, makes the system unusable without network and an AWS account, and forecloses the
  privacy-preserving mode without evidence that it is inadequate.
- **Stay fully local and ignore Bedrock.** Rejected by the owner, and it would leave R2 unmitigated.
- **Prompt-and-parse for Bedrock JSON.** Rejected: MEASURED to return fenced JSON, so it would
  reintroduce exactly the parse-failure mode the frozen schemas exist to eliminate.

## Verification

- `us.anthropic.claude-haiku-4-5-20251001-v1:0` invoked successfully from this host on 2026-09-14
  (~1.6 s). The bare model id `anthropic.claude-haiku-4-5-20251001-v1:0` returns
  `ValidationException: ... on-demand throughput isn't supported` — the inference profile id is
  required, and this is recorded so nobody rediscovers it.
- Acceptance for this ADR: both providers pass the same `LLMProvider` contract tests, and P4-T02
  publishes a side-by-side table for both on the frozen benchmark set.
