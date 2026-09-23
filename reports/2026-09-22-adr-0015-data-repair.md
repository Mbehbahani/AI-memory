# ADR-0015 deferred data repair — 2026-09-22

## Scope

This is the one-time, non-destructive correction deferred by ADR-0015 §4. It changes only the
original 2026-09-17 cohort; it is not a re-extraction and does not change source documents.

## Safety snapshot

Before the correction, the complete `facts` table was copied to
`facts_pre_adr0015_20260922` in PostgreSQL. The snapshot contains 1,935 rows.

## Measured correction

The original cohort contained 60 incorrectly functional closures:

| Predicate | Rows re-opened |
|---|---:|
| `HAS_OWNER` | 31 |
| `USES_ARCHITECTURE` | 24 |
| `DEPLOYED_ON` | 5 |
| **Total** | **60** |

For each row, the repair set `valid_to = NULL`, `status = 'current'`,
`invalidated_at = NULL`, and `invalidated_by_episode_id = NULL`.

`Ontology.orient()` then identified and reversed 32 unambiguously backward `HAS_OWNER` facts in
the same original cohort. Each corrected fact has the orientation reason appended to its
`statement` as `[ADR-0015 direction normalized: ...]`. There is no dedicated fact-column for the
reason; ADR-0015 explicitly permits the statement/provenance record.

The 92 named corrections are 92 correction events across 67 unique fact rows: 25 ownership rows
were both re-opened and direction-normalized.

## Verification

- Original-cohort historical rows for the three predicates: **0**.
- Original-cohort `HAS_OWNER` rows for which `Ontology.orient()` still returns `flipped=True`: **0**.
- Live functional unique index contains only `HAS_STATUS`, `HAS_STAGE`, and `SELECTED_OPTION`.
- `aimemory-ingest rebuild-graph` completed successfully after the repair with zero drops and zero
  errors. Its `closed_edges` count changed from 151 before repair to 91 after repair, exactly the
  60 re-opened rows. Node/relationship projection totals were unchanged because the repair changes
  temporal fields and direction, not fact cardinality.

## Deliberate exclusions

Historical rows created after 2026-09-17 were not modified. The 73 facts closed by the
`senior-ai-eng` semantic-edge reviews remain a separate semantic-quality review track.
