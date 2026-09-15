# ADR-0014 — Claude Haiku 4.5 is the default extraction provider; one model per corpus

- **Status:** accepted
- **Date:** 2026-09-15
- **Deciders:** owner (Mohammad — *"select the best approach and prevent mismatch … use the best approach"*), orchestrator A00
- **Amends:** [ADR-0012](ADR-0012-dual-llm-provider-evaluation.md) §2 (which left the default at `ollama` pending measurement)
- **Related:** [ADR-0009](ADR-0009-knowledge-engine-verdict.md), [ADR-0010](ADR-0010-evaluation-protocol.md), [ADR-0005](ADR-0005-temporal-rules.md)
- **Evidence:** [`reports/local-ai-validation.md`](../../reports/local-ai-validation.md) — P4-T02, n=20 per provider

## Context

ADR-0012 built two providers and deliberately declined to rank them until measured. P4-T02 has now
measured both on 20 identical real my-vault episodes against the frozen schemas:

| Metric | `qwen3:4b` (local) | Claude Haiku 4.5 (Bedrock) | Threshold |
|---|---|---|---|
| first-pass JSON validity | 95.0 % | 95.0 % | ≥ 70 % (stop rule) — both pass |
| mean entity recall | 68.9 % | 68.9 % | C3 ≥ 60 % — both pass |
| **mean relationship recall** | **40.2 %** | **61.2 %** | **C3 ≥ 50 % — local FAILS, Bedrock passes** |
| entity-type correctness | 85.7 % | 91.8 % | — |
| median s / episode | 174.43 | 7.74 | — |
| median s / relationship call | 347.12 | 5.08 | — |
| cost / episode | $0 | $0.0083 | — |

Schema validity and entity recall are **ties** — to one decimal place on entity recall. The
discriminator is **relationship extraction**, where the local model scores 40.2 % against the plan's
own ≥ 50 % bar (§O, C3). Relationships are what make this a knowledge graph rather than a list of
nouns; a graph missing 60 % of its edges does not support the multi-hop questions the system exists
to answer.

The owner, having seen these numbers and been told explicitly that Bedrock transmits vault content to
AWS, instructed: select the best approach, and prevent mismatch.

## Decision

### 1. Bedrock Claude Haiku 4.5 is the default extraction provider

`LLM_PROVIDER=bedrock` becomes the shipped default, via inference profile
`us.anthropic.claude-haiku-4-5-20251001-v1:0` in `us-east-1`. Full-vault extraction: ESTIMATED
**~$1.66** and ~30 minutes, against ESTIMATED 6–12 hours locally at a quality level that fails C3.

`qwen3:4b` and `OllamaProvider` are **retained, maintained, and tested** — not deprecated. They are
the offline mode, the zero-cost mode, and the answer to "what happens when AWS is unreachable". The
Ollama service stays in the compose stack. ADR-0009's native engine is provider-agnostic precisely so
this remains true.

### 2. One extraction model per corpus — the mismatch rule

This is the substance of "prevent mismatch". The two models **disagree systematically on entity
types**, not randomly (MEASURED): the vault's PARA folders are typed `Repository` by qwen3 and
`InfrastructureComponent` by Haiku; both should be `Document|Concept`. A graph built half by each
would contain two incompatible typings of the same thing, and no amount of provenance stamping fixes
a graph whose nodes disagree about what they are.

Therefore:

- **`ingestion_runs` records the `extraction_model_id` used.** Every `fact`, `entity_mention` and
  `knowledge_artifact` already stamps it (plan §J) — that stays, and is now load-bearing rather than
  informational.
- **The ingestion pipeline refuses to extract into a project whose existing current facts were
  produced by a different `extraction_model_id`.** It fails with a clear message naming both models
  and the remedy. This is a hard guard, not a warning.
- **The remedy is an explicit re-extraction**, not a silent mix: `aimemory-ingest reprocess
  --re-extract --model <id>` re-runs extraction for the affected scope under one model and supersedes
  the old facts through the normal ADR-0005 temporal path (old facts become `historical`, never
  deleted). Provenance then shows both generations honestly.
- **`--allow-model-mix` exists as an escape hatch** for benchmarking only. It is never the default,
  and using it is recorded on the run.

### 3. Deterministic seeding beats both models where both are wrong

Both providers mistyped the same things. Where a correct answer is already known, **no model gets a
vote**:

- The PARA folder names (`00 Inbox`, `01 Projects`, `02 Areas`, `03 Resources`, `04 Archives`,
  `05 Templates`, `06 Outputs`, `07 Workflows`) are seeded as `Document`/`Concept` entities at Tier 0.
- Projects from the registry (`AIOS/me.md`, `AIOS/Maps/project-graph.md`) are seeded as `Project`
  with `extraction_model_id = deterministic:registry-v1`, as §J already specifies.
- **Entity type is rejected at write time when it contradicts a deterministic seed.** A07a/A08
  enforce this; the LLM may add entities, never retype a seeded one.

This is the cheapest available quality win: it removes the single largest category of type error from
both providers without touching either model.

### 4. Secret-suspected sources are never transmitted

Already true, now security-critical rather than hygienic: a `secret_suspected` source is
metadata-only and has no stored text, so there is nothing to send. With a cloud provider as the
default, the secret detector is a **privacy control on egress**. A07a asserts this in a test; A13
verifies it in P15.

## Consequences

- **Assumption B16 no longer holds in the default configuration.** Vault content — including personal
  notes, CVs and job applications — is transmitted to AWS Bedrock during extraction. AC-8 ("nothing
  leaves the machine") is now true only under `LLM_PROVIDER=ollama`. `README.md`, `docs/operations/`
  and `docs/security/` must state the default plainly and say how to switch. A13's P15 threat model
  treats egress as a first-class item. **This is the cost of the decision and it is not hedged.**
- **The stack needs AWS credentials to do its default job.** The `ingestion` container gets them from
  the standard boto3 chain — a read-only mounted `~/.aws`, or environment. Never baked into an image,
  never logged, never committed. `scripts/doctor.ps1 -Security` gains a check.
- **A network outage degrades extraction, not the system.** Retrieval, embeddings, the graph and MCP
  are all local and unaffected. Switching `LLM_PROVIDER=ollama` restores extraction offline at lower
  relationship quality — and per rule 2, into a *separate* corpus scope or behind a re-extraction.
- **ADR-0010's model-upgrade rule now has a live comparison to anchor on.** The frozen benchmark set
  has real numbers for two models; a third candidate is scored the same way.
- **Both providers stay in CI.** The contract tests run against both. If the local path rots
  unnoticed, the offline mode is a fiction.

## Rejected alternatives

- **Keep `ollama` as the default.** Rejected: it fails C3 on relationship recall (40.2 % vs the ≥ 50 %
  bar), which the plan itself set. Shipping a default that fails the plan's own quality gate — when a
  measured alternative passes it — would be choosing the worse system.
- **Mix providers per tier** (cheap local pass, cloud for priority documents). Rejected: this is
  precisely the mismatch the owner asked to prevent, and the measured type disagreements make it
  concretely harmful, not theoretically untidy.
- **Drop Ollama now that it is not the default.** Rejected: it is the offline and zero-cost mode, it
  already works, and ADR-0009's engine is provider-agnostic specifically to keep it viable. Removing
  it would make the AWS dependency unconditional.
- **Use a larger local model (`qwen3:8b`).** Not rejected — untested. ESTIMATED ~2× slower than
  `qwen3:4b`, which is already 174 s/episode, and RAM already peaks at 11.37 GiB against a 15.46 GiB
  ceiling. It is a candidate for ADR-0010's benchmark process, not a V0.1 default.
