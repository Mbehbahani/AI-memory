# ADR-0013 — Neo4j Community cannot enforce a read-only user; access is constrained in the client instead

- **Status:** accepted
- **Date:** 2026-09-14
- **Deciders:** A04 (discovery and evidence), orchestrator A00
- **Amends:** plan §T ("Neo4j read-only user"), §Q ("access boundaries — Neo4j read-only user"), §S
  ("Both use the read-only Neo4j user")
- **Related:** [ADR-0001](ADR-0001-postgres-system-of-record.md),
  [ADR-0007](ADR-0007-loopback-exposure.md), [ADR-0011](ADR-0011-ops-dashboard.md)

## Context

The approved plan specifies a read-only Neo4j user (`memory_reader`) used by memory-api, NeoDash and
Neo4j Browser, so that a read path cannot mutate the graph. `infra/neo4j/schema/readonly-user.cypher`
was scaffolded with a `GRANT ROLE reader TO memory_reader` line.

While implementing P5-T02, A04 established empirically on the running `neo4j:5.26-community`
container that this does not work:

- `GRANT ROLE ...` and `SHOW ROLES` both fail with **"Unsupported administration command"**.
  Role-based access control is a Neo4j **Enterprise** feature.
- A user created **with no role grant at all** successfully executed a `CREATE`.

So on Community Edition every authenticated user is effectively a full-privilege user. The
`memory_reader` credential in `.env` is a *separate identity*, which is still useful for audit and for
revoking one consumer without rotating the other, but it is **not** a privilege boundary. Claiming
otherwise in the threat model would be false.

Upgrading to Enterprise is out of scope for V0.1: it is a commercial licence, and the plan
(§E, ADR-0001) deliberately treats Neo4j as a rebuildable projection precisely so that it never has to
be the trusted component.

## Decision

**Do not claim a database-enforced read-only boundary on Neo4j in V0.1.** Replace it with three
layers that are actually true, and state the limitation plainly wherever the plan previously promised
the user:

1. **Client-side write refusal.** `Neo4jGraphStore.query()` rejects any Cypher containing a write
   clause. The read paths (memory-api, the `/ops` page) reach Neo4j only through it, so a read path
   cannot issue a write even by accident or by prompt injection into a generated query.
2. **The graph is rebuildable (ADR-0001).** Postgres is the system of record. A corrupted or mutated
   graph is recovered with `scripts/rebuild-graph.ps1`, not from a backup. The blast radius of an
   unauthorised Neo4j write is a projection that gets regenerated — never a lost fact.
3. **Network containment (ADR-0007).** Bolt and HTTP are published on `127.0.0.1` only. In V0.1 the
   only principals on that loopback are the owner and the containers. There is no remote access.

`memory_reader` is still created and still used by the read consumers — as an identity for audit and
credential separation, explicitly **not** as an enforcement mechanism.

## Consequences

- **`readonly-user.cypher` no longer contains the non-functional `GRANT ROLE` line.** Leaving it in
  would have failed silently on some paths and created a false sense of enforcement.
- **A13 (P15) must treat this as a real finding, not a footnote.** The threat model must say: *any
  process that can reach Bolt on loopback with any valid credential can write to the graph.* The
  mitigations above are what stands between that and harm, and their adequacy rests entirely on
  ADR-0007 (loopback-only) holding.
- **Documentation must stop promising a read-only user.** `README.md`, `docs/operations/queries.md`
  and `docs/security/` must state the limitation. A14 and A13 own those edits.
- **NeoDash is unconstrained.** It takes arbitrary Cypher from its dashboard definition and does not
  go through `GraphStore`, so layer 1 does not protect it. It is started only under
  `--profile viz` and reads from loopback; that is the whole mitigation, and it should be stated.
- **If V0.1 ever grows remote access, this decision must be revisited first.** The multi-device
  boundary named as out-of-scope in §AG cannot be crossed while the graph has no privilege model —
  that would need Enterprise, or a proxy that enforces read-only ahead of Bolt.

## Rejected alternatives

- **Neo4j Enterprise.** Commercial licence; disproportionate for a single-user local V0.1; and
  ADR-0001 already removes the need for Neo4j to be trusted.
- **Pretend the user is read-only and move on.** Rejected outright: the plan's own rule is that every
  number and every claim is labelled honestly. A security control that does not exist is worse than a
  documented absence, because the threat model would be built on it.
- **Drop `memory_reader` entirely.** Rejected: separate identities still give audit separation and
  independent rotation, which are worth keeping even without enforcement.

## Verification

- `GRANT ROLE` / `SHOW ROLES` → "Unsupported administration command" on `neo4j:5.26-community`
  (MEASURED 2026-09-14).
- A role-less user executed `CREATE` successfully (MEASURED 2026-09-14) — this is the proof that the
  boundary does not exist.
- `tests/integration/test_graph_store.py` asserts `Neo4jGraphStore.query()` refuses write clauses.
  A13 must confirm in P15 that no read path bypasses `GraphStore`.
