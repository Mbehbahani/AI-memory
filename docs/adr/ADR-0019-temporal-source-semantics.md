# ADR-0019 — Temporal source semantics

Status: proposed · Date: 2026-09-22

## Context

ADR-0005 rule 3 currently reconciles an edited source as if it were a complete snapshot: a fact
from the preceding version that is absent from the new extraction becomes `unconfirmed` (it remains
valid, is ranked lower, and is never deleted). That is correct for a replacement document, but not
for a changelog, discussion, or append-only log: absence in those sources says nothing about an
earlier assertion.

No source classification exists today in the PostgreSQL schema, domain models, source-root config,
or temporal engine. This ADR is deliberately a design and implementation plan only. It makes no
database, model, temporal-data, or runtime behaviour change.

## Decision

Introduce a required source-level classification named `source_type` with this closed vocabulary:

```yaml
source_type: snapshot | delta | discussion | note | append_only
```

It belongs on `sources`, not `source_versions`: it describes how a source is interpreted across
versions. It is provenance metadata and must be available when the temporal engine reconciles an
old and a new version.

| Source type | Meaning of a fact absent from a newer version |
| --- | --- |
| `snapshot` | Apply the existing ADR-0005 rule 3 reconciliation: mark the previous fact `unconfirmed`; retain `valid_to = NULL`. Explicit contradiction and explicit supersession retain their existing authority. |
| `delta` | No temporal action from absence. The new version is an incremental statement, not a full replacement. |
| `discussion` | No temporal action from absence. Conversation omission is not a withdrawal. |
| `note` | No temporal action from absence. A note can be partial or selectively edited. |
| `append_only` | No temporal action from absence. Prior facts remain valid; appended content may add or explicitly supersede/contradict facts. |

`snapshot` is the default for every new or unclassified source. This deliberately preserves current
behaviour and ensures a rollout cannot silently relax existing temporal reconciliation.

“Invalidate” for a `snapshot` means the existing, non-destructive ADR-0005 treatment: status
`unconfirmed`, not deletion and not a closed validity interval. This ADR does not alter the rules
for functional predicates, explicit supersession, contradictions, source deletion, or `as_of`.

## Data and API design

1. Add `SourceType(StrEnum)` in the domain with exactly the five values above.
2. Add `sources.source_type` as a non-null PostgreSQL text/check-constrained column, default
   `snapshot`. Expose it in the `Source` domain model, repository reads/inserts, source DTOs, and
   source-detail API responses.
3. Allow source-root configuration to provide an optional default source type. A per-source
   classification takes precedence; omitted configuration resolves to `snapshot`.
4. Keep `source_type` stable for a source unless an operator intentionally reclassifies it. Record
   a `source_events` event (`source_type_changed`) with old and new values and the actor/reason.
5. Each reconciliation event must include the source type actually applied. This makes a later
   timeline explain why absence did or did not produce an `unconfirmed` fact.

Reclassification is forward-looking only. It must not mass-change statuses or validity intervals of
existing facts. It governs the next modified-version reconciliation. A retrospective repair, if
ever requested, is a separate bounded data-correction task with a snapshot and explicit approval.

## Temporal-engine change (future implementation)

The only decision point is `reconcile_version(old_version, new_version, new_result, episode)`:

```text
IF source.source_type == snapshot:
    apply existing absence loop (mark unmatched, non-contradicted facts unconfirmed)
ELSE:
    do not infer any status change from unmatched prior facts

# In all source types, still apply new extracted facts, explicit supersession,
# functional-predicate rules, and explicit contradictions normally.
```

This branch must be made before the existing missing-fingerprint loop. It must not be implemented
as a retrieval filter, a graph-only rule, or a deletion rule. PostgreSQL remains the system of
record; Neo4j receives the resulting fact statuses through the normal rebuild/projection path.

## Non-destructive migration plan (future implementation)

1. Take a verified PostgreSQL backup and record pre-migration counts for `sources`, current facts,
   unconfirmed facts, and facts grouped by source status. Do not alter fact rows in this migration.
2. Add nullable `sources.source_type` with the five-value check constraint, then backfill every
   existing row to `snapshot` in bounded batches. Verify no null or out-of-vocabulary values remain.
3. Set the server default to `snapshot`, make the column `NOT NULL`, and add the domain/repository/
   API/config wiring. Existing callers that omit the field must continue to create `snapshot`
   sources.
4. Deploy the engine branch only after the column and defaults are live. Initially classify no
   existing sources differently; this proves the rollout has no behavioural effect.
5. Classify selected sources through an operator-reviewed inventory. For each intentional change,
   emit `source_type_changed`; do not re-run historic reconciliation merely to apply the label.
6. Validate on an isolated fixture corpus first, then one approved pilot source per non-snapshot
   type. Compare fact statuses before and after; rebuild Neo4j only if facts actually changed.
7. Roll back code by treating all sources as `snapshot`. Do not remove the column or alter history
   during incident rollback. The classification data is additive provenance and is safe to retain.

## Required test cases (future implementation)

| Test | Setup | Expected result |
| --- | --- | --- |
| Legacy/default compatibility | Existing source row and a new source created without a type; edit removes one previously extracted fact. | Both resolve to `snapshot`; that fact becomes `unconfirmed`, with `valid_to IS NULL`, exactly as ADR-0005 does now. |
| Snapshot reconfirmation | A `snapshot` source removes a fact, then adds it back in a later version. | It changes `current → unconfirmed → current`; no duplicate fact and no closed validity interval. |
| Delta omission | A `delta` source's later version omits an earlier fact and adds a new one. | Earlier fact stays `current`; new fact is added; no absence-driven source event marks it unconfirmed. |
| Discussion and note omission | Run the same omission fixture once for each type. | Prior fact remains `current` in both cases. |
| Append-only omission | An `append_only` source's new version has only a later entry. | All prior open facts remain current; newly stated facts are added. |
| Rules still apply to non-snapshots | For every non-snapshot type, submit explicit supersession, explicit contradiction, and a functional-predicate replacement. | Existing ADR-0005 / ADR-0015 outcomes occur; source type suppresses only absence inference. |
| Per-source precedence | Root default is `delta`; a source is explicitly `snapshot`, and vice versa. | The source's value controls reconciliation. |
| Migration safety | Migrate a fixture database with untyped sources and existing current/unconfirmed/historical facts. | All sources become `snapshot`; fact values and counts are byte-for-byte unchanged. |
| Constraint and DTO validation | Try null/unknown values, then inspect API/domain serialization. | Database rejects invalid persisted values; API exposes only the closed vocabulary; omitted input yields `snapshot`. |
| Reclassification audit | Change a source from `snapshot` to `delta`, then modify it. | A `source_type_changed` event records old/new/reason; the next omission leaves the fact current; no historical statuses are rewritten. |
| Projection and retrieval fidelity | Rebuild after each scenario and query with default and `as_of` retrieval. | Neo4j matches PostgreSQL status/validity; unconfirmed warning/penalty applies only to snapshot-created unconfirmed facts. |

## Consequences

This avoids false loss-of-confidence for incremental and conversational sources without weakening
the conservative existing behaviour of all unclassified sources. It adds classification work and
requires operator care: a wrongly labelled snapshot can leave stale facts current, while an
unlabelled delta remains safely conservative as `snapshot`.

## Related

ADR-0001 (PostgreSQL system of record) · ADR-0005 (temporal rules) · ADR-0015 (predicate
cardinality) · `docs/architecture/temporal.md` §§3–6.
