# Start message for the development session

Before sending: start Docker Desktop, then open Claude Code in `D:\AI memory`
(terminal: `cd "D:\AI memory"` then `claude --add-dir "D:\My-Vault" --add-dir "D:\AWS2\SupaBaseProject\DE"`).

Copy everything inside the block below as the first message. Edit only the AC-9 line if you want the
12 GB Docker cap.

```text
START DEVELOPMENT
Project: D:\AI memory. Read CLAUDE.md, then docs/architecture/v0.1-plan.md, docs/adr/README.md, and docs/development/task-plan.md.
Decisions: AC-1 to AC-8, AC-11 and AC-12 accepted as recommended. AC-9: no. AC-10: no.
Docker Desktop is running. Run phases P0 to P17 without stopping, using the agents in .claude/agents/.
```

If you want the Docker memory cap instead, replace the AC-9 part with:
`AC-9: yes, cap WSL2 at 12GB (create C:\Users\PC\.wslconfig).`
