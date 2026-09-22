# ADR-0018 — Luna relay provider for Tier 2 extraction

Status: accepted · Date: 2026-09-21 · Extends ADR-0016

## Context

The owner requested that the `D:\Apply files` corpus use the Codex GPT-5.6 Luna subagent for Tier 2
extraction instead of AWS Bedrock. The existing relay protocol already provides the required safe
handshake: prompts are written to `.relay/requests`, answers are read from `.relay/responses`, and
answers are schema validated before the native engine receives them.

## Decision

Add `LLM_PROVIDER=luna`. It reuses the relay provider implementation and the normal native engine,
entity resolver, temporal rules, `KnowledgeWriter`, and graph projection. Its registered provenance
identity is `codex:gpt-5.6-luna` (`provider=codex`, route `codex-luna-subagent`). The identity is
distinct from both Bedrock and the historical Claude relay route.

No extraction call is made until the source has completed Tier 1 and the operator has run the Luna
relay handshake. Missing responses leave the episode recoverable and do not create facts.

## Consequences

- Every persisted fact can be attributed to Luna through `extraction_model_id`.
- The existing DTO, JSON Schema, ontology, temporal, and persistence safeguards remain in force.
- Extraction is an operator workflow that requires answering generated relay requests. The native
  engine may issue a second relationship prompt when the first answer contains no facts.
- A corpus already owned by another extraction model must use the existing explicit re-extraction path;
  silent model mixing remains prohibited.

## Operating Luna roots

Run Tier 1 normally, then use `LLM_PROVIDER=luna` with a root-scoped Tier 2 command for
`apply-files`, `apply-files-interview`, `content-center`, or `wagtail`. Answer the JSON requests in
`.relay/requests` as a Codex
Luna subagent and place schema-valid responses in `.relay/responses`. Requeue failed episodes for the
same root and run Tier 2 again until all relationship and optional retry requests are answered.

```powershell
docker compose --profile tools run --rm -e LLM_PROVIDER=luna tools aimemory-ingest tier2 --root apply-files
docker compose --profile tools run --rm -e LLM_PROVIDER=luna tools aimemory-ingest reprocess --failed --root apply-files
docker compose --profile tools run --rm -e LLM_PROVIDER=luna tools aimemory-ingest tier2 --root apply-files
```

Use the relevant root id in place of `apply-files` for other roots. A pending relay
response marks an episode failed; the reprocess command requeues it without repeating Tier 1.
The general ingestion worker still uses its configured provider, which is Bedrock by default; it
does not automatically invoke a Codex subagent for later changes to these roots.

The `wagtail` root belongs to the existing `oploy-website` project, which previously held Haiku
facts from a vault note. For its initial Luna migration, use the project-scoped
`reprocess --re-extract --model codex:gpt-5.6-luna --project oploy-website --run` command for each
relay pass. This is the model guard's explicit migration path; audit the earlier Haiku facts and
close any that remain current after Luna extraction rather than leaving a mixed-model project.
