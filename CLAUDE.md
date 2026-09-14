# AI Memory — project instructions for Claude Code sessions

This repository is the **AI Memory V0.1** build (local Docker Compose). Read in this order:

1. `docs/architecture/v0.1-plan.md` — the approved plan (architecture, data model, ontology, temporal
   and provenance rules, retrieval, MCP, security, resources).
2. `docs/adr/README.md` — decisions already made; never reverse one silently (write a superseding ADR).
3. `docs/development/task-plan.md` — task ids P0-T01 … P17-T02, owners, acceptance, status.
4. `docs/development/agent-matrix.md` and `docs/development/agent-protocol.md` — who owns which
   paths; result format every agent must return.

## Execution rules

- Development starts only on the explicit instruction **START DEVELOPMENT**; then run P0 → P17
  automatically. Stop only for: destructive action outside `D:\AI memory`, missing access, an invalid
  major assumption, or a required major technology change.
- The orchestrator (this session) dispatches work to the agents in `.claude/agents/` (Opus for design/
  integration, Sonnet for scaffolding/tests/docs) and forwards every result to `build-observer`.
- Source roots (`D:\My-Vault`, `D:\AWS2\SupaBaseProject\DE`, …) are **read-only**. Never write there.
- Never `docker compose down -v`, never delete volumes, never bind ports to `0.0.0.0`.
- Never print or commit secrets. `.env` stays local; `.env.example` is the template.
- Do not replace Qwen3 4B, MiniLM, Postgres, Neo4j, or Ollama with anything else. If a choice fails,
  record evidence and report a BLOCKER.
- Label every number MEASURED / DOCUMENTED / ESTIMATED / UNKNOWN. Never invent metrics.
- Commit at phase ends with the attribution line required by the session; do not push (no remote in V0.1).

## Stack cheat-sheet

`docker compose up -d` (base + dev override) · memory-api `127.0.0.1:8000` · MCP `127.0.0.1:8020/mcp` ·
Neo4j Browser `127.0.0.1:7474` · NeoDash `127.0.0.1:5005` (profile `viz`) · tests via profile `tools`.
