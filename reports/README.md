# Reports (Build Observer output)

Maintained by A01 (`.claude/agents/build-observer.md`). Nothing here is fabricated; missing values are
written as `not available`.

| File | Content |
|---|---|
| `agent-runs.jsonl` | One JSON object per task (schema in the agent definition) |
| `task-ledger.md` | Task id · phase · agent · status · files · tests · handoff · commit |
| `build-timeline.md` | Chronological phases/tasks with start/end and blockers |
| `test-summary.md` | Every test run with verbatim failures and their fixes |
| `environment-audit.md` | P0 audit output (re-run at START DEVELOPMENT) |
| `image-pins.md` | Image tags + digests pinned in P3 |
| `local-ai-validation.md` | P4 measurements (Qwen3 4B, MiniLM) |
| `graphiti-gate.md` | P4 gate results per criterion |
| `pilot-validation.md` | P13 graph validation for JobLab DE |
| `evaluation-<ts>.md` | Gold-set evaluation runs |
| `deploy-verification.md` | P16 clean-start and health verification |
| `final-build-report.md` · `known-limitations.md` · `next-steps.md` | P17 deliverables |
