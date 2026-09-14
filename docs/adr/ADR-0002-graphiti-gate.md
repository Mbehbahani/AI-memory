# ADR-0002 — Graphiti adopted only through a measured compatibility gate with automatic fallback

Status: accepted · Date: 2026-09-13

## Context
Graphiti-core is the preferred temporal-graph framework but assumes OpenAI-shaped providers and issues
6–10 LLM calls per episode. The runtime is CPU-only with Qwen3 4B via Ollama and MiniLM (384-d).
Compatibility and latency are unknown until measured.

## Decision
A `KnowledgeEngine` port isolates the engine. In P4, A05 runs the gate defined in the plan (§O):
C1 schema reliability ≥ 90 %; C2 median ≤ 240 s and p90 ≤ 480 s per ~800-token episode; C3 ≥ 60 %
expected entities and ≥ 50 % expected relationships on a hand-listed set; C4 supersession fixture
works; C5 two concurrent episodes complete; C6 384-d storage + indices created. All pass →
`GraphitiEngine`; any fail → `NativeTemporalEngine` (our prompts, Ollama JSON-schema decoding,
`schemas/extraction/*`). The verdict is recorded in `reports/graphiti-gate.md` and ADR-0009 without
stopping development. The Graphiti MCP server is not used.

## Consequences
+ No irreversible dependency; same Postgres/Neo4j conventions either way. − If the gate fails, the full
native extraction path must be built (planned for). − If it passes, the native path exists only as a
stub plus the deterministic structural graph.
