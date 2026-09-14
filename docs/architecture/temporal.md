# Temporal Memory Model (V0.1)

Status: **implementable spec**, frozen with the P1 contract · Author: A02 · Date: 2026-09-14
Authority: **ADR-0005** (rules 1–6) and plan section I · Consumer: **A08** (P8-T01/T02/T03), with
A04 (columns and the partial unique index), A09 (`as_of` filtering and the unconfirmed penalty),
A07a (change detection that triggers rule 3), A12 (memory scenarios).
Code: `aimemory.common.time`, `aimemory.ontology.is_functional`, `aimemory.domain.models.Fact`.

The single sentence this document exists to protect: **the memory never forgets; it only changes what
is currently true.**

## 1. The two time axes

| Axis | Column | Meaning | Set from |
|---|---|---|---|
| **Valid time** | `valid_from`, `valid_to` | when the statement is/was true *in the world* | an explicit date in the text, else `observed_at` |
| **Observation time** | `observed_at` | when *we* learned it | file mtime or git commit time when available, else the ingestion-run start time |

ADR-0005 §6, implemented by `observed_at_for(source_time, run_started_at)` and
`valid_from_for(stated, observed_at)`. `valid_to IS NULL` means "still current". Every value is
timezone-aware UTC; a naive datetime entering the system is treated as UTC by `ensure_utc`.

## 2. Point-in-time query (ADR-0005 rule 5)

```sql
WHERE valid_from <= :as_of AND (valid_to IS NULL OR valid_to > :as_of)
```

`valid_to` is **exclusive**: a fact closed at `T` is not valid *at* `T`. The Python twin is
`aimemory.common.time.is_valid_at(valid_from, valid_to, as_of)` and
`tests/unit/test_contracts.py::test_point_in_time_predicate` pins the boundary.

Neo4j equivalent (edges carry the same two properties):

```cypher
WHERE r.valid_from <= datetime($as_of)
  AND (r.valid_to IS NULL OR r.valid_to > datetime($as_of))
```

## 3. Status vocabulary

| `facts.status` | `valid_to` | Meaning | Retrieval treatment |
|---|---|---|---|
| `current` | NULL | observed in the newest version of its source, not contradicted | normal |
| `unconfirmed` | NULL | **still current**, but not re-extracted from the newest version (rule 3) | ranked down by `boosts.unconfirmed_penalty` (−0.15) and flagged in the context block |
| `historical` | set | closed by a newer contradicting fact or an explicit supersession | excluded unless `as_of` falls inside the window |

`facts.source_status` is orthogonal: `deleted` marks a fact whose source file is gone (rule 4). The
fact keeps its validity; the Gateway flags it and the Ops "attention list" surfaces it.

`knowledge_artifacts.current_status` uses the richer `ArtifactStatus` set; `superseded` is a **state**,
not a deletion (rule 6).

## 4. Supersession algorithm

This is the exact procedure A08 implements in `knowledge/temporal/`. It runs once per
`ExtractionResult`, *after* entity resolution and *inside* the same transaction as the fact insert.

```text
FUNCTION apply_fact(new_fact, episode, ontology, repo):
  # new_fact has: subject_entity_id, predicate, object (entity id or literal),
  #               valid_from, observed_at, confidence, provenance

  # ---- 0. explicit supersession wins over everything (ADR-0005 rule 2) -----------------
  IF episode states an explicit supersession for this subject+predicate
     OR the artifact carried supersedes_if_stated
     OR the caller passed record_decision(supersedes=<id>):
        target := resolve(stated target)                       # by artifact id, else by title
        IF target EXISTS:
            close(target, at = new_fact.valid_from, by_episode = episode.id,
                  reason = "explicit")
            new_fact.supersedes_fact_id := target.id
            insert(new_fact, status = current)
            link_SUPERSEDES(new_fact, target)
            RETURN Applied(explicit)
        ELSE:
            record_warning("stated supersession target not found"); fall through

  # ---- 1. functional predicates: one current object (ADR-0005 rule 1) ------------------
  IF ontology.is_functional(new_fact.predicate):
        open := repo.find_open_fact(subject = new_fact.subject_entity_id,
                                    predicate = new_fact.predicate)   # valid_to IS NULL
        IF open IS NULL:
            insert(new_fact, status = current); RETURN Applied(first_value)

        IF object_key(open) == object_key(new_fact):
            # same value re-observed: refresh, do not create a second row
            repo.touch(open, observed_at = max(open.observed_at, new_fact.observed_at),
                       status = current,                        # clears an 'unconfirmed' flag
                       confidence = max(open.confidence, new_fact.confidence))
            RETURN Applied(reconfirmed)

        IF new_fact.valid_from < open.valid_from:
            # a late-discovered older statement must not close a newer one
            insert(new_fact, status = historical,
                   valid_to = open.valid_from)
            RETURN Applied(backdated)

        close(open, at = new_fact.valid_from, by_episode = episode.id, reason = "functional")
        new_fact.supersedes_fact_id := open.id
        insert(new_fact, status = current)
        link_SUPERSEDES(new_fact, open)
        RETURN Applied(superseded)

  # ---- 2. non-functional predicates accumulate ----------------------------------------
  existing := repo.find_open_fact(subject, predicate, object_key(new_fact))
  IF existing IS NOT NULL:
        repo.touch(existing, observed_at = max(...), status = current)
        RETURN Applied(reconfirmed)
  insert(new_fact, status = current)
  RETURN Applied(added)


PROCEDURE close(fact, at, by_episode, reason):
  ASSERT at >= fact.valid_from                     # never close before it opened
  IF fact.valid_to IS NOT NULL AND fact.valid_to == at:
        RETURN                                     # idempotent: re-running an episode is a no-op
  repo.update(fact,
              valid_to        = at,
              status          = historical,
              invalidated_at  = now_utc(),
              invalidated_by_episode_id = by_episode)
  graph.invalidate_relationship(fact.id, at)       # SET r.valid_to; the edge is never deleted
```

`object_key` is `str(object_entity_id)` when the object is an entity, otherwise `object_value`
(`Fact.object_key` in the domain model).

`KnowledgeEngine.invalidate(fact_id, at, by_episode)` is the port-level entry point for step
`close()`; it must be idempotent for exactly the reason shown above — the ingestion state machine may
replay the `temporal` stage after a crash.

### Database backstop

`uq_facts_functional_current` (see `data-model.md` §3) is a partial unique index on
`(subject_entity_id, predicate) WHERE valid_to IS NULL AND predicate IN <functional list>`. If a bug
ever tried to leave two open functional facts, the insert fails instead of corrupting the timeline.

## 5. Rule 3 — source edits never delete knowledge

When A07a detects a **modified** source (same URI, new hash) it creates a new version, a
`document_change` episode, and re-extracts. Afterwards A08 runs:

```text
PROCEDURE reconcile_version(old_version, new_version, new_result, episode):
  previous := repo.facts_from_version(old_version)           # status IN (current, unconfirmed)
  reextracted := { fingerprint(f) for f in new_result.facts }   # (subject, predicate, object_key)

  FOR f IN previous:
      IF fingerprint(f) IN reextracted:
          CONTINUE                                # apply_fact() already reconfirmed it
      IF contradicted_by(f, new_result):          # functional predicate, different object
          CONTINUE                                # apply_fact() already closed it
      repo.update(f, status = unconfirmed)        # valid_to stays NULL - still current
      emit_source_event(old_version.source_id, 'modified', {fact_id: f.id, to: 'unconfirmed'})
```

Only a contradiction or a user action closes an unconfirmed fact. A fact that is re-observed later
returns to `current` through the `reconfirmed` branch of `apply_fact`.

Knowledge artifacts follow the same rule on `current_status`.

## 6. Rule 4 — source deletion flags, never erases

```text
PROCEDURE mark_source_deleted(source, episode = NULL):
  repo.update(source, status = 'deleted')
  emit_source_event(source.id, 'deleted')
  repo.update_all(facts      WHERE source_id = source.id, source_status = 'deleted')
  repo.update_all(artifacts  WHERE source_id = source.id, source_status = 'deleted')
  # valid_from / valid_to / status are NOT touched
```

A **moved** source (the same hash reappears at another path in the same root) keeps its `source_id`,
updates `uri` and `moved_from_uri`, writes a `moved` event, and triggers **no** re-extraction — the
knowledge and its provenance stay attached to the same source row.

If a deleted file comes back, the source returns to `active` with a `restored` event and the flags
are cleared.

## 7. Where each rule is enforced

| ADR-0005 rule | Enforced in | Verified by |
|---|---|---|
| 1 functional supersession | `apply_fact` step 1 + `uq_facts_functional_current` | `tests/memory` "conflicting fact", unit tests on `is_functional` |
| 2 explicit supersession is authoritative | `apply_fact` step 0; `supersedes_if_stated`; `record_decision(supersedes=…)` | "superseded decision" scenario, Architecture-A/B fixture |
| 3 unconfirmed, not deleted | `reconcile_version` | "modified" scenario |
| 4 deletion flags | `mark_source_deleted` | "deleted" scenario |
| 5 `as_of` predicate | `is_valid_at` + the SQL/Cypher clause | unit test on boundaries; "timeline" scenario |
| 6 artifact statuses are states | `knowledge_artifacts.current_status` + `SUPERSEDES` chain | `memory.get_artifact` returning the chain |

## 8. Timeline and `as_of` reads (what A09 must implement)

* `GET /v1/timeline?project_id=&entity=&from=&to=` merges three ordered streams:
  facts (`valid_from`, and `valid_to` as a second "closed" event), artifacts (`valid_from` /
  supersession), and `source_events`. Each item carries provenance.
* Every read path that returns facts or artifacts accepts `as_of` and, when absent, uses `now()`.
* `since` filters on `observed_at` (what changed recently), **not** on `valid_from`. The two are
  different questions and are never silently swapped.

## 9. The supersession fixture (C4 of the ADR-0002 gate)

`tests/fixtures/mini-vault/architecture-decision-a.md` and `-b.md` are the frozen pair. Expected
outcome after ingesting A then B:

1. the A decision artifact has `current_status = superseded` and `valid_to = B.valid_from`;
2. the B artifact is `current` with `supersedes_id = A.id`;
3. a `SUPERSEDES` edge exists from B to A in Neo4j, and A's edge has `valid_to` set;
4. `as_of` between A and B still returns A as current;
5. every one of those rows passes `Provenance.is_complete`.

Any engine that fails this fails gate criterion C4 regardless of its latency.

## Deviations

1. **Backdated facts are explicitly handled.** ADR-0005 does not say what happens when a
   newly-observed fact has a `valid_from` *older* than the open fact. The rule fixed here — insert it
   as `historical` with `valid_to = open.valid_from`, do not close the newer fact — prevents a late
   scan of an old document from rewriting the present.
2. **A `reconfirmed` branch is specified.** The plan implies re-extraction clears `unconfirmed`; the
   algorithm now states that re-observing the same value refreshes the existing row instead of
   inserting a duplicate, which also keeps the functional unique index satisfiable.
3. **`close()` is defined as idempotent**, because the ingestion state machine may replay the
   `temporal` stage after an interruption (plan section L).
4. **`facts.source_status` is a column**, not a join to `sources.status`. The plan says "marks derived
   facts `source_status=deleted`"; storing it keeps the retrieval filter to a single table.
5. **`since` is defined against `observed_at`.** The plan lists a `since` parameter without fixing its
   axis; using observation time makes "what changed lately" answerable even for documents that state
   old dates.

## Related

`data-model.md` §3 (columns, the partial unique index) · `ontology.md` §4 (functional predicates) ·
`retrieval.md` §5 (temporal filter and the unconfirmed penalty) · ADR-0005, ADR-0002, ADR-0006.
