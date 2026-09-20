# ADR-0017 — Keep imperfect knowledge, marked, rather than discard it cleanly

Status: accepted · Date: 2026-09-19 · Amends ADR-0014 rule 2 and the projection side of ADR-0015

## Context

Two rules, both sound in their original setting, were each costing more than they protected once the
corpus was real.

**One model per corpus (ADR-0014 rule 2).** The rule exists because `qwen3:4b` and Claude Haiku 4.5
disagree *systematically* about entity types, so a corpus extracted by both is internally
inconsistent. The guard compares `extraction_model_id` strings. ADR-0016 added a second route to the
*same* Haiku 4.5 (`claude-code:haiku-4-5` alongside `bedrock:us.anthropic.claude-haiku-4-5-…`), and
the string comparison could not tell "a different model" from "the same weights down another wire".
Adding anything under the new route would have flagged **1,245 vault facts `unconfirmed`** — the full
cost of a model change, with none of its cause, and a −0.15 retrieval penalty on the whole vault
until a re-extraction that would have cost ≈2.7M tokens.

**Endpoint types (ADR-0015).** The projection dropped any edge whose endpoint labels broke the
contract. MEASURED 2026-09-19: **676 of 5,256 semantic edges — 13 %** — were refused. `Person
-[HAS_OWNER]-> Project` (backwards, 22), `Concept -[PART_OF]-> Concept` (23), ≈160 other shapes. The
facts survived in Postgres, so nothing was lost; they were *invisible*, which is worse than wrong.
Invisible knowledge cannot be reviewed, cannot be repaired, and surfaces only as a graph that looks
thin for no discoverable reason.

The owner's instruction (2026-09-19), during a testing stage: complete the work without an expensive
re-extraction, and push every extracted fact into the graph.

## Decision

**1. Equivalent model routes are one model.** `ingest_repo.EQUIVALENT_MODEL_GROUPS` lists ids naming
the same underlying model. `flag_other_model_facts` never flags across a group, and
`extraction_models_in_use` collapses a group to its largest member so `status` does not report a
mixed corpus. ADR-0014 rule 2 is otherwise unchanged: `qwen3:4b` and Haiku 4.5 remain incompatible.

**2. Ontology-violating edges are projected and marked, not dropped.** Under
`GRAPH_ALLOW_ONTOLOGY_VIOLATIONS` (default **false**; set true here) the edge is written carrying
`ontology_violation`, naming the rule it breaks. Both validation points honour it — the projection
and `Neo4jGraphStore.upsert_relationships` — because an edge the projection deliberately kept must
not die silently at the write.

**Marked, not laundered.** One predicate excludes them:

```cypher
MATCH (a)-[r]->(b) WHERE r.ontology_violation IS NULL RETURN a, r, b
```

Nothing claims a backwards edge is well formed. A missing endpoint is still dropped — there is no
node to write to.

## Consequences

**+** MEASURED after: `dropped: 0`, edges **4,580 → 5,253**, 676 marked. Search 70 ms, 23 related
entities on a sample query (was 19). Zero facts flagged `unconfirmed`; the 5 that were are confirmed.
**+** The 676 malformed edges are now *reviewable*. Repairing predicate direction is a query away
instead of an archaeology exercise against Postgres.
**+** No re-extraction: ≈2.7M tokens and an hour of degraded vault answers avoided.

**−** The graph now contains edges that violate its own ontology. Any consumer that assumes
well-formedness must filter on `ontology_violation`; NeoDash dashboards and saved queries have not
been audited for this.
**−** `Person -[HAS_OWNER]-> Project` in the graph is still wrong. Marking makes it visible and
survivable, not correct. Repairing direction remains open work, and the 676 are the worklist.

## The standing policy this sets

Recorded in `CLAUDE.md`: **"add X" means add, not rebuild.** Never re-extract an already-extracted
corpus to satisfy bookkeeping; keep imperfect knowledge with a marker rather than discarding it;
rebuild and report measured before/after numbers. What is relaxed is throwing work away — not
telling the truth about its quality.

Tests: `tests/unit/test_ontology_violation_projection.py`.
