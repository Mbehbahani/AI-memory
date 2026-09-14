# mini-vault fixture

A tiny vault used by unit, memory-scenario, and gate tests. A07b completes it in P6-T01 (10 notes with
frontmatter and wikilinks). The two `architecture-decision-*.md` notes are the canonical supersession
fixture (Architecture A selected 2026-09-01 → abandoned 2026-09-10 → Architecture B selected 2026-09-11)
used by the Graphiti gate (C4) and the temporal tests. Expected outcome after ingesting both:

- Decision A: `valid_from=2026-09-01`, `valid_to=2026-09-10`, `current_status=superseded`
- Decision B: `valid_from=2026-09-11`, `valid_to=NULL`, `current_status=current`
- Relationship `B -SUPERSEDES-> A`
- `get_timeline(project=fixture-project)` lists three events in order, each with its source URI and hash.
