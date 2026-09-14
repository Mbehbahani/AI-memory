// Read-only credential for memory-api and NeoDash. Applied by `aimemory-ingest migrate` against the
// `system` database, with $readonly_user / $readonly_password bound from NEO4J_READONLY_USER /
// NEO4J_READONLY_PASSWORD. Idempotent: `IF NOT EXISTS` makes a re-run a no-op.
//
// KNOWN LIMITATION (verified against the running neo4j:5.26-community image, 2026-09-14, A04/P5-T02):
// Neo4j Community Edition has no role-based access control. `SHOW ROLES` and `GRANT ROLE ... TO ...`
// both fail with "Unsupported administration command" on Community - RBAC (custom or built-in roles,
// GRANT/REVOKE privileges, `dbms.security.*` role procedures) is an Enterprise-only feature. A user
// created with CREATE USER on Community has exactly the same access as every other user: full read
// and write. There is no database-enforced way to make `memory_reader` actually read-only on this
// edition, and CLAUDE.md/agent-protocol forbid substituting a different technology (e.g. upgrading to
// Enterprise) to work around it.
//
// This was proven directly: a freshly created Community-edition user with no role grant successfully
// ran `CREATE (n:TestWriteCheck {id:1})` and the write succeeded.
//
// Mitigation (recorded for ADR-0007/§T and A13's P15 security review):
//   1. The credential still exists and is handed to memory-api/NeoDash so the *intent* (principle of
//      least privilege at the connection level) is documented and future Enterprise migration is a
//      config change, not a rewrite.
//   2. Enforcement moves up a layer: `Neo4jGraphStore.query()` (packages/aimemory/persistence/
//      graph_store.py) refuses any Cypher containing a write clause (CREATE/MERGE/DELETE/SET/REMOVE/
//      DROP) before it reaches the driver, and the Gateway/NeoDash code paths only ever call
//      `query()`/`neighbours()`, never `upsert_*`/`clear`. No application code path issues a write
//      over this credential.
//   3. ADR-0001: Neo4j is a rebuildable projection of PostgreSQL, never the sole holder of a fact. If
//      this credential were ever misused to write or corrupt graph state, `scripts/rebuild-graph`
//      regenerates the whole projection from PostgreSQL with no data loss - the blast radius of a
//      compromised "read-only" credential is bounded to a projection that can be thrown away and
//      rebuilt, not to the system of record.
// Accepted risk, not a blocker: V0.1 runs on a single local machine behind loopback-only ports
// (ADR-0007), so the credential is not exposed to any other host in the first place.

CREATE USER $readonly_user IF NOT EXISTS SET PASSWORD $readonly_password CHANGE NOT REQUIRED;
