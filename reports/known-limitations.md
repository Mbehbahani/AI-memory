# Known limitations

Limitations that are **accepted for V0.1** — deliberate, documented, and not defects. Each entry says
what the limitation is, what stands in for the missing control, and what would have to change for it
to stop being acceptable.

Defects that should be *fixed* do not belong here; they belong in a finding with an owner.

Index maintained by A01. Sections are added by the agent that established the limitation.

---

## Security (A13, P15-T01, 2026-09-17)

Full context: [`docs/security/threat-model.md`](../docs/security/threat-model.md) and
[`docs/security/checklist-2026-09-17.md`](../docs/security/checklist-2026-09-17.md).

### SEC-L1 — No authentication on any endpoint

memory-api `/v1/**` and `/ops`, the MCP server on 8020, Neo4j Browser, NeoDash and Bolt are all
unauthenticated to anything that can reach loopback. **Accepted** per ADR-0007 and ADR-0011: one user,
one machine, `127.0.0.1` only (verified live — every published port and every host listening socket is
`127.0.0.1`). **Stops being acceptable the moment any remote access exists**, including a tunnel or a
reverse proxy.

### SEC-L2 — Neo4j has no privilege boundary (ADR-0013)

Neo4j Community has no role-based access control. Re-verified 2026-09-17: `SHOW ROLES` returns
*"Unsupported administration command"*, and `memory_reader` successfully executed a write. Any process
that reaches Bolt on loopback with any valid credential can write the graph. `memory_reader` is an
**identity** for audit and independent rotation, **not** an enforcement mechanism.

What stands in for it: `Neo4jGraphStore.query()` refuses write clauses and no user-controlled Cypher
reaches it (all five call sites pass module-level constants with bind parameters); the graph is a
rebuildable projection (ADR-0001), so the blast radius of an unauthorised write is
`scripts/rebuild-graph.ps1`, never a lost fact; and Bolt is loopback-only.

Caveat for the future: APOC is installed (190 procedures). A write procedure outside the
`create|merge|remove` naming convention would not match the write-clause regex. Irrelevant today
because nothing accepts caller-supplied Cypher — **and a reason not to add an endpoint that does**
without revisiting ADR-0013 first.

### SEC-L3 — Vault content is transmitted to AWS Bedrock by default (ADR-0014)

Assumption B16 ("no cloud calls at runtime") and AC-8 ("nothing leaves the machine") are true only
under `LLM_PROVIDER=ollama`. In the shipped default, episode text — including motivation letters
naming real employers, job applications, a personal profile and a skill map — is sent to
`us.anthropic.claude-haiku-4-5-20251001-v1:0` in `us-east-1`. This was an explicit owner decision made
with the numbers in hand (local relationship recall 40.2 % against the plan's own ≥ 50 % bar).

Verified as protected from egress: sources flagged `secret_suspected` are excluded twice — the
pipeline never creates an episode for one, and `claim_episode_for_extraction` filters them in SQL. The
one flagged source in the current corpus has zero stored text, chunks, episodes and blobs. Embeddings
are local and the embedding image is built offline (`HF_HUB_OFFLINE=1`).

### SEC-L4 — Nothing is encrypted at rest

`pg_data`, `neo4j_data` and `backups/` (which contains a plaintext copy of `.env`, required for
restore) sit unencrypted on `D:`. So does `D:\My-Vault`, so encrypting the derivative alone would be
theatre. Enabling BitLocker on `D:` closes all of them at once and is the cheapest available
improvement. `backups/*` is git-ignored, so none of it can be committed.

### SEC-L5 — Extraction is not hardened against prompt injection

A hostile document in a source root can attempt to steer what the LLM extracts. The mitigation is
structural rather than semantic: output is validated against frozen schemas, entity types cannot
contradict a deterministic seed (ADR-0014 §3), external content is stamped `origin=external,
trust=low`, and every fact carries provenance. A poisoned fact is therefore *visible and supersedable*,
not silent. A dedicated injection-detection pass is out of scope for V0.1.

### SEC-L6 — No dependency supply-chain verification

No SBOM, no hash-pinned lockfile, no `pip-audit` in the test loop. Image digests are recorded in
`reports/image-pins.md`. This is the realistic route by which the AWS-credential exposure (SEC-01 in
the threat model) would turn into an incident, and it is the thing to fix first if that finding is not
closed by scoping the IAM principal.

### SEC-L7 — Write payloads and search queries are retained verbatim

`mcp_audit_log.arguments` stores the arguments of every MCP write attempt, including refused ones, and
also emits them as a log line; `retrieval_logs.query_text` retains every search string indefinitely
(178 rows today). Both are by design — ADR-0008 requires the audit trail and ADR-0010 requires the
retrieval log — but both are personal data at rest in an unencrypted volume (see SEC-L4).

---

### Open findings that are *not* accepted limitations

These have owners and should be fixed; they are listed here only so the two lists are not confused.

| Id | Severity | Owner | Summary |
|---|---|---|---|
| SEC-01 | High | owner (AWS console) | The mounted `~/.aws` profile has `AdministratorAccess` + `IAMFullAccess`, not `bedrock:InvokeModel`. Scope a dedicated IAM principal. |
| SEC-02 | Medium | A09 / A16 | `/ops` actions accept unauthenticated cross-origin form POSTs (no CSRF token, no `Origin` check, no `TrustedHostMiddleware`). A visited web page can enqueue a paid ingestion run. |
| SEC-03 | Medium | A10 | The MCP audit sink latches to `local` on any transient memory-api error and never recovers; 25 of 32 records, including every write refusal, were dropped. |
| SEC-06 | Low | A09 | `mcp_audit_log.arguments` has no size cap and `POST /v1/mcp/audit` has no rate limit. |
