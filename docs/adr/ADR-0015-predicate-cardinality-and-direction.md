# ADR-0015 — Predicate cardinality and direction are part of the ontology contract

Status: accepted · Date: 2026-09-17 · Author: A02
Refines ADR-0005 (rule 1 is unchanged; what changes is which predicates it applies to). Does **not**
supersede it. Amends `schemas/ontology.yaml` (version `0.1.0` → `0.2.0`), `ontology.md` §2–§4,
`temporal.md` §4, `data-model.md` §3.

## Context

The P14 gold-set evaluation against the full vault corpus (614 entities, 1,244 facts, MEASURED
2026-09-17) surfaced two defects that produce **wrong** knowledge, not merely incomplete knowledge.

**1. `functional_predicates` collapsed genuinely concurrent values.** ADR-0005 rule 1 closes the
previous object of a functional predicate as `historical`. MEASURED:

| predicate | current | historical | of total |
|---|---|---|---|
| `USES_ARCHITECTURE` | 22 | 24 | 46 |
| `HAS_OWNER` | 27 | 31 | 58 |
| `DEPLOYED_ON` | 36 | 5 | 41 |

For `JobLab` specifically, 5 objects were current and 13 historical; `Python` and `PostgreSQL` were
filed as history while both are concurrently true. "What does JobLab use" silently omitted them.
60 facts in total are closed only because rule 1 was applied to a multi-valued predicate.

**2. Extracted facts ran opposite to the declared direction.** `ontology.md` declared `Project` as
the subject of `HAS_OWNER`. MEASURED: of 58 `HAS_OWNER` facts, 38 had a `Person` as subject
(`Mohammad Behbahani HAS_OWNER JobPilot`); all 7 `HAS_STATUS` facts likewise (`Mohammad HAS_STATUS
Picnic` — a job application mis-typed as a status). Nothing rejected them, because
`Ontology.validate_relationship` returned early for functional predicates: *"functional predicates
are attribute-like; endpoints are not constrained."* The two defects compounded — each new project
mention for the same person closed the previous one.

The cause of (2) is a gap in enforcement, not in intent. The cause of (1) is a genuine modelling
error: the original list was assembled from the plan's examples without a stated membership test.

## Decision

**1. A predicate is functional only when two concurrent values would be a contradiction, not merely
unusual.** The asymmetry justifies the conservative side: an over-permissive call leaves a stale
value visible next to the true one (cheap, visible, reversible); an over-restrictive call rewrites a
true current fact as `historical` and deletes it from every answer (expensive, invisible).

Applying the test:

| Predicate | Functional? | Reasoning |
|---|---|---|
| `HAS_STATUS` | **yes** | A thing has exactly one current status; that is what "status" means. |
| `HAS_STAGE` | **yes** | One stage at a time; advancing is exactly the supersession rule 1 describes. |
| `SELECTED_OPTION` | **yes** | A decision has one option in force; choosing another supersedes it. |
| `HAS_OWNER` | **no** | Co-ownership is ordinary, not contradictory (repositories, organizations). |
| `USES_ARCHITECTURE` | **no** | A project is built on several technologies at once — the defect above. |
| `DEPLOYED_ON` | **no** | Multi-target and multi-region deployment are ordinary. |

The three demoted predicates become ordinary `relationship_types.semantic` edges (19 → 22 edge
types, 6 → 3 functional predicates). Supersession is not lost for them: ADR-0005 rule 2 (explicit
supersession stated in the text) and rule 3 (`unconfirmed` when a new source version stops stating a
value) apply, and a choice that genuinely replaces its predecessor is modelled as a `Decision` whose
`SELECTED_OPTION` changed.

**2. Direction is enforced at write time, in the ontology layer.** `from`/`to` are a direction, not a
set of participating labels, and that applies to functional predicates too. Functional predicates
declare no `from` list — their legal subjects are exactly the node types naming them in
`node_types.<T>.functional`, and the loader enforces the correspondence both ways. They declare `to`,
where a new `literal` sentinel means "the object is a value in `facts.object_value`, not an entity"
(`HAS_STATUS: {to: [literal]}` is what rejects `Mohammad HAS_STATUS Picnic`).

**3. An unambiguously reversed triple is repaired, not dropped.** `Ontology.orient(predicate,
from_label, to_label)` returns the triple in the declared direction:

* legal as written → unchanged, `flipped=False`;
* illegal as written, legal reversed → reversed, `flipped=True` and a `reason` (MEASURED: this
  repairs 32 of the 58 `HAS_OWNER` facts, rejects 0);
* legal both ways (`Person HAS_OWNER Person`) → unchanged; ambiguity is never resolved by guessing;
* illegal both ways → `OntologyError`; the fact is dropped with a recorded reason.

A flip must be **recorded, not silent**: A08 emits it as an ingestion-run warning and keeps
`Orientation.reason` with the fact's `statement`, so `explain` can show that the system, not the
document, chose the direction. `facts` has no column for it today; adding
`facts.direction_normalized boolean` is optional future work and would need an A04 migration — it is
deliberately not required here, because a warning is enough to make the behaviour auditable.
A literal object is never flipped — a value cannot become a subject.

**4. The existing 1,244 facts are left in place; a non-destructive repair is proposed separately.**
See below. Nothing in this ADR rewrites stored data.

## What this is *not*

The direction half **restores stated intent**: `ontology.md` already declared `Project → HAS_OWNER →
Person`, and the corpus violated it only because nothing checked. The novel decisions here are the
membership test (1), the enforcement point (2) and the auto-flip (3); the direction itself is not new
and no earlier decision is reversed.

## Consequences

+ Concurrently-true facts stop being rewritten as history; the graph's `HAS_OWNER`/`USES_ARCHITECTURE`
  edges point the declared way; a reversed triple is repaired instead of lost.
− Stricter validation drops facts that are illegal in both directions (MEASURED: ~6 of 46
  `USES_ARCHITECTURE`, ~11 of 41 `DEPLOYED_ON`, 7 of 7 entity-valued `HAS_STATUS`). They remain in
  PostgreSQL and are recorded in the projection's `dropped` list with a reason — the third outcome
  `test_every_fact_is_an_edge_a_property_or_a_recorded_drop` already asserts.
− Until the `uq_facts_functional_current` index is narrowed (A04), a second concurrent
  `USES_ARCHITECTURE` fact for one subject raises `IntegrityError` at insert.
  `tests/integration/test_contracts_functional_index.py` holds that gap open as a strict `xfail`.
− Multi-valued predicates now rely on rules 2 and 3 for supersession, which is weaker inference than
  rule 1 was. That is the intended trade: weaker inference over confident, wrong inference.

## Repairing the existing corpus (proposed, not performed)

The corpus cost ~35 minutes of paid extraction; 60 facts are wrongly `historical` and 32 `HAS_OWNER`
facts point backwards. Three options:

| Option | Cost | Risk | Reversible? |
|---|---|---|---|
| **A. Leave as-is** | zero | 60 true facts keep answering as history; 32 answer backwards. The defect persists for every existing document until it is re-ingested. | n/a |
| **B. Targeted SQL repair** (recommended) | ~1 h to write and review; seconds to run | Bounded: two `UPDATE`s over ≤92 rows, each with an exact `WHERE`. Main risk is re-opening a fact that a *genuine* rule-2 supersession closed — excluded by requiring `supersedes_fact_id IS NULL` on the closing row and `reason='functional'` on the closure. | Yes, with a `facts` snapshot taken first |
| **C. Re-extract the corpus** | ~35 min of paid LLM time, plus non-determinism | Highest: a re-run produces a *different* fact set, so the gold set and every MEASURED number in `reports/` stop referring to the same corpus. Does not fix facts whose source is no longer present. | No |

**Recommendation: B, gated on A04's index migration and A08's `orient` wiring**, in this order:

1. A04 narrows `uq_facts_functional_current` to the three predicates. (Must be first: step 3 re-opens
   facts the six-predicate index would reject.)
2. A08 wires `Ontology.orient` into `knowledge/persist.py` and the projection, so new extractions
   stop reproducing the defect. (Must precede 3, or the repair is undone by the next run.)
3. Snapshot `facts` (`CREATE TABLE facts_pre_adr0015 AS SELECT * FROM facts`), then:
   * **re-open wrongly closed facts** — `valid_to = NULL`, `status = 'current'`,
     `invalidated_at = NULL`, `invalidated_by_episode_id = NULL` for rows where
     `predicate IN ('HAS_OWNER','USES_ARCHITECTURE','DEPLOYED_ON')` and the closure was rule 1
     (`status='historical'` and no explicit supersession recorded);
   * **flip reversed facts** — swap `subject_entity_id`/`object_entity_id` where
     `Ontology.orient` reports `flipped=True`, and record the flip in the statement/provenance so it
     is explainable. Run it through `orient` rather than hand-written SQL, so the ontology stays the
     only place direction is defined.
4. `scripts/rebuild-graph` (Neo4j is a rebuildable projection, ADR-0001 — no graph migration needed).
5. Re-run the P14 gold set and compare against `reports/evaluation-20260917T193016Z.md`.

Step 3 is a data migration and is **not** part of this task; it needs Mohammad's explicit go-ahead
and a verified snapshot first.

## Related

ADR-0005 (temporal rules; rule 1 unchanged) · ADR-0001 (the graph is rebuildable, so only PostgreSQL
needs repairing) · `docs/architecture/ontology.md` §3–§4 · `docs/architecture/temporal.md` §4 ·
`docs/architecture/data-model.md` §3 · `reports/evaluation-20260917T193016Z.md` (the measurements).
