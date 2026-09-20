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
- Source roots (`D:\My-Vault`, `D:\AWS2\SupaBaseProject\DE`, …) are **read-only**. Never write there,
  with two narrow exceptions granted by the owner on 2026-09-20:
  - **Session notes may be written to `D:\My-Vault\01 Projects\AI Memory\`.** Those notes are the
    only way a session's conclusions become *searchable text* — an MCP write produces facts but no
    chunks and no embeddings, so its sentences can never be found by meaning-search.
  - **A project row may be appended to `AIOS\Maps\project-graph.md`** when the owner asks for a
    project to be added. Nothing else in that file may be edited, and never a row the owner wrote.
    The registry is the one thing the LLM may not contradict, so touching it is deliberate and
    additive only: `default_project_id` is a foreign key, so a root cannot be labelled until its
    project exists here.

  Everything else in the vault stays untouched.
- Never `docker compose down -v`, never delete volumes, never bind ports to `0.0.0.0`.
- Never print or commit secrets. `.env` stays local; `.env.example` is the template.
- **Testing stage (set by the owner, 2026-09-19).** The stack is open to substitution: Qwen3 4B,
  MiniLM, Postgres, Neo4j and Ollama are defaults, not constraints. Try another model, provider or
  store when there is a reason to. Two conditions remain:
  - Register what you use. Every fact points at a row in `extraction_models`, so a new extractor gets
    its row *before* it writes, never after. An unregistered writer is still a BLOCKER — it makes
    "which model said this?" unanswerable, which is the whole point of provenance.
  - Record the swap and its evidence in an ADR superseding ADR-0009/0012/0014 as applicable. The
    earlier ADRs stand as history; they simply stop being binding.
- **Cloud extraction is permitted while testing.** The owner has stated the ingested content is not
  sensitive at this stage. Ingest a project the way the owner asks; do not re-open the privacy
  question each time. Revisit only if the corpus later takes in client work or personal records.
- **"Add X" means add, not rebuild (owner, 2026-09-19).** When the owner asks to add a project, a
  root or a batch of knowledge, do the cheap additive thing and keep what is already there:
  - **Never re-extract an already-extracted corpus to satisfy a bookkeeping rule.** ADR-0014 rule 2
    exists because `qwen3:4b` and Haiku 4.5 genuinely disagree about entity types. Two *routes to the
    same model* do not (see `ingest_repo.EQUIVALENT_MODEL_GROUPS`); add a new id to that group rather
    than flagging a corpus `unconfirmed`.
  - **Keep imperfect knowledge, marked.** An edge that breaks the ontology's endpoint contract is
    projected with `ontology_violation` rather than dropped (`GRAPH_ALLOW_ONTOLOGY_VIOLATIONS`). A
    reader can exclude it with one predicate; a dropped edge cannot be reviewed or repaired at all.
  - Rebuild the graph afterwards and **report the measured before/after numbers**, not adjectives.
  - Still refuse to make something *silently* wrong: marking, flagging and logging stay. What is
    relaxed is throwing work away, not telling the truth about it.
- Label every number MEASURED / DOCUMENTED / ESTIMATED / UNKNOWN. Never invent metrics.
- Commit at phase ends with the attribution line required by the session; do not push (no remote in V0.1).

## Stack cheat-sheet

`docker compose up -d` (base + dev override) · memory-api `127.0.0.1:8000` · MCP `127.0.0.1:8020/mcp` ·
Neo4j Browser `127.0.0.1:7474` · NeoDash `127.0.0.1:5005` (profile `viz`) · tests via profile `tools`.
