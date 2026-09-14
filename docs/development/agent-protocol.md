# Development Agent Protocol

Every development agent in `.claude/agents/` follows this protocol. Agents are build workers only;
they are not components of the runtime system.

## Boundaries
- Edit only the paths listed under **Owned files** in your definition. If a change is needed elsewhere,
  stop and report `NEEDS_HANDOFF: <path> — <reason>` in your result instead of editing.
- Contracts in `schemas/` and `packages/aimemory/domain` are frozen after P1. Only A02 changes them,
  and only with an ADR.
- Never read, print, or copy secrets. Never write into source roots (`D:\My-Vault`, project repos).
  Never run `docker compose down -v` or delete volumes. Never change files outside `D:\AI memory`.
- Do not switch models, providers, or major technologies. If the assigned choice fails, record the
  evidence and report `BLOCKER: <what> — <evidence> — <proposed alternatives>`.
- Do not fabricate measurements. Label every number MEASURED / DOCUMENTED / ESTIMATED / UNKNOWN.

## Working style
- Read `docs/architecture/v0.1-plan.md`, the ADRs, and your task rows in
  `docs/development/task-plan.md` before writing code.
- Match the surrounding code style (ruff, line length 100, typed Python 3.12, pydantic v2).
- Write tests next to the feature (see `tests/`); run them; include the raw pass/fail output in your result.
- Keep commits to the orchestrator; do not run `git commit` yourself unless your definition says so.

## Result format (returned to the orchestrator; forwarded verbatim to the Build Observer)
```
TASK: <task id> — <title>
STATUS: done | partial | blocked
STARTED / FINISHED: <ISO timestamps you observed>
FILES_CREATED: [...]
FILES_MODIFIED: [...]
COMMANDS_EXECUTED: [...]
TESTS_EXECUTED: <command> → <passed>/<failed> (raw summary lines)
ERRORS / WARNINGS: [...]
ARCHITECTURE_DECISIONS: [...] (or "none")
MEASUREMENTS: [...] with labels (or "none")
HANDOFF_TO: <agent id> — <what they need to know>
UNFINISHED: [...]
```
