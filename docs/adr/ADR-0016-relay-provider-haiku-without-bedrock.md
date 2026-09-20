# ADR-0016 — A third extraction route: Claude Haiku 4.5 without Bedrock

Status: accepted · Date: 2026-09-19 · Supersedes nothing; extends ADR-0012 and ADR-0014

## Context

ADR-0014 chose AWS Bedrock (Claude Haiku 4.5) as the extraction provider on MEASURED quality:
relationship recall 61.2 % against `qwen3:4b`'s 40.2 %, where the plan's criterion C3 requires ≥ 50 %.
The local model fails the project's own quality bar; Bedrock clears it.

That left no acceptable option for an operator who does not want extraction billed to AWS in a given
session. `ollama` is free and private but produces a measurably worse graph. Bedrock is good but is
the thing being avoided. The owner asked for a third route on 2026-09-19: the *same* Haiku 4.5,
answered by a Claude Code subagent in the operator's session.

The owner also relaxed the technology lock in `CLAUDE.md` the same day, making a substitution
permissible provided the model is registered and the swap is recorded here.

## Decision

Add `LLM_PROVIDER=relay`, implemented at the `LLMProvider` seam
(`aimemory.providers.llm.relay_provider`). A subagent cannot be called from inside a container, so the
call is inverted:

1. hash the prompt, its schema and its system prompt into a stable key;
2. return the answer if `<relay>/responses/<key>.json` exists;
3. otherwise write `<relay>/requests/<key>.json` and raise `RelayPending`.

The operator answers the requests between runs; extraction is re-run and the identical prompts hash
to identical keys, so answered calls now succeed. Generation is `temperature=0`, so identical prompt
means identical intended answer and a key hit is a cache hit rather than a correctness risk.

Registered as `claude-code:haiku-4-5`, **distinct from** `bedrock:us.anthropic.claude-haiku-4-5-…`
even though the underlying model is the same. `extraction_models` answers *how* a fact was produced,
and a cost or incident review needs to know which route a fact came down.

ADR-0014 rule 2 (one extraction model per corpus) is unchanged. Switching a corpus to this route uses
the sanctioned remedy: `aimemory-ingest reprocess --re-extract --root <root> --model claude-code:haiku-4-5`,
which re-queues the episodes and flags the previous generation's facts `unconfirmed` — never deletes.

## Consequences

**+** The quality of Haiku 4.5 with no AWS call, so AC-8 ("nothing leaves the machine") is still
violated but by a different route the operator controls directly.
**+** Implemented at the provider seam, so entity resolution, the ADR-0005 temporal rules, the
registry veto and provenance are untouched — only the model connection changed.
**+** Prompts and answers are on disk and inspectable, which is the clearest view of what the model is
actually asked that this system has ever had.

**−** Two passes per episode instead of one, and extraction cannot run unattended.
**−** MEASURED 2026-09-19: ≈ 20k tokens and ≈ 30 s of subagent time per prompt. joblab-de's 61
episodes need ≈ 122 prompts; the vault's 149 need ≈ 298. Bedrock does the same work in minutes for
≈ $1.25. **This route is roughly 100× slower and consumes operator session budget rather than money.**
It is a deliberate choice for a session, not a new default.
**−** `.relay/` is a bind mount, read-write, holding full document text. It is git-ignored and must
stay that way.

## Two bugs found while building it

Both pre-existed this ADR and are fixed alongside it.

1. **Silent truncation.** `MAX_BODY_CHARS` was a hardcoded 8000, sized for `qwen3:4b`'s 8192-token
   context, and never revisited when Bedrock became the default. MEASURED: 74 of 210 episodes (35 %)
   exceeded it; the largest was 73,213 characters, so 88 % of it never reached the model — silently,
   on the longest documents. The budget is now derived from the provider
   (`LLMSettings.resolved_max_body_chars`) and any truncation is logged and recorded on the episode.

2. **A failed relationship call counted as success.** Call 2 raising left the episode `valid=True`,
   so it kept its entities, left the queue and never got another attempt — with no relationships,
   permanently. A single Bedrock timeout could cost a document its whole contribution to the graph.
   "The model found none" and "nobody asked the model" produce the same empty list; only the first is
   a result. Now `valid=False`, so the episode stays queued.

Tests: `tests/unit/test_relay_provider.py`, `tests/unit/test_episode_truncation.py`,
`tests/unit/test_relationship_call_failure.py`.
