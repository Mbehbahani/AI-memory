# ADR-0005 — Temporal memory rules

Status: accepted · Date: 2026-09-13

## Decision
1. Functional predicates (`schemas/ontology.yaml: functional_predicates`): a new fact with the same
   subject+predicate and a different object closes the old fact (`valid_to = new.valid_from`,
   `status=historical`) and links `SUPERSEDES`. Non-functional predicates accumulate.
2. Explicit supersession in text, or `record_decision(supersedes=…)`, is authoritative over inference.
3. Source edits never delete knowledge: facts from a superseded source version that are not
   re-extracted become `status=unconfirmed` (still `valid_to NULL`, ranked lower, flagged). Only a
   contradiction or a user action closes them.
4. Source deletion flags derived facts (`source_status=deleted`); nothing is erased.
5. `as_of` queries use `valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)`.
6. `observed_at` is the source time (mtime/commit) when available, else the run time; `valid_from`
   defaults to `observed_at` unless the text states a date.

## Consequences
+ History and current state coexist; supersession is queryable. − `unconfirmed` requires clear
labelling in context and a periodic review query (risk R6).
