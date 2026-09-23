# AI Memory stabilization report — 2026-09-22

## 1. Executive Summary

**Current status: conditionally usable; no-go for wider ingestion.** PostgreSQL remains canonical and Neo4j is a populated projection. Hybrid retrieval, graph expansion, provenance, and the live MCP audit path work. Earlier claims that Neo4j was empty or that audit recovery was unproven are outdated.

The original ADR-0015 92-fact correction is complete. Do not broaden ingestion until the JobLab DE pilot’s failed persistence operation, semantic relationship over-claims, and entity duplicates are handled. SEC-02 remains open and out of scope.

**Labels:** **MEASURED** = this session’s query/test; **DOCUMENTED** = cited but not re-tested; **UNKNOWN** = not established.

## 2. Verified Resolved Issues

| Previous concern | Why outdated/fixed | Evidence |
| --- | --- | --- |
| Neo4j was empty | The manager package was stale. | **MEASURED live:** **7,251 nodes**, **13,150 relationships**. `Source=1,160`, matching PostgreSQL `sources=1,160`; PostgreSQL has `entities=1,885`, `facts=2,083`, `knowledge_artifacts=3,397`, `episodes=997`. |
| Rebuild was unproven | Two same-session rebuilds emitted identical writer totals. | **MEASURED:** each reported 6,826 nodes / 12,398 relationships; per-label/type readback matched. See the counter caveat in §3. |
| ADR-0015’s original 92 facts were wrong | The bounded, non-destructive data correction ran after a full facts snapshot. | **MEASURED:** reopened `HAS_OWNER=31`, `USES_ARCHITECTURE=24`, `DEPLOYED_ON=5`; normalized 32 backward ownership facts. Original cohort now has 0 target-predicate historical facts and 0 still-flippable ownership facts. [Repair evidence](2026-09-22-adr-0015-data-repair.md). |
| Functional cardinality could recur | Functional predicates are config-driven and the database index matches. | **MEASURED:** live index contains only `HAS_STATUS`, `HAS_STAGE`, `SELECTED_OPTION`; ontology/index suite: 25 passed. |
| MCP audit could stop after an API blip | The real Docker test exercised a refused write before and after restart. | **MEASURED:** both rows persisted; final MCP health was `sink=gateway`, `degraded=false`, `dropped=0`. See §7. |
| Writes were off | Both gates are already live. | **MEASURED:** `/health`: `mcp_write_enabled=true`, `gateway_write_enabled=true`, `writes.effective=true`. |
| Provider, ontology, MIRROR needed redesign | All are correct as implemented. | **MEASURED:** factory supports ollama/bedrock/relay/luna with distinct provenance; ontology loader enforces YAML predicates; opt-in MIRROR uses PostgreSQL `mirror_blobs`, no MinIO/S3. |

## 3. Remaining Issues

| Severity | Impact | Recommendation |
| --- | --- | --- |
| High | **JobLab DE is not ready for wider ingestion:** 1/61 episodes failed due to nullable `project_id` persistence; sampled edges over-claim semantics; 36 duplicate normalized-name groups contain 42 extra entities. | Fix/retry `docs/14-phase-3-silver-relationships.md`, review/repair semantic edges, and establish a repeatable review gate. Maintain no-go. |
| Medium | **SEC-02, out of scope:** cross-origin unauthenticated form POSTs can enqueue `/ops` paid runs. | Dedicated CSRF/Origin/host-protection change and security re-test. [Threat model](../docs/security/threat-model.md#sec-02--ops-actions-are-unauthenticated-cross-origin-reachable-form-posts--medium). |
| Medium | Rebuild report discrepancy: writer said 12,398 relationships while direct/readback total was 12,385 in both idempotency runs. | Reconcile writer-attempt vs actual-projection count; do not rebuild just for this metric. |
| Medium | **92-fact repair (2a): resolved, not an active repair queue.** 92 events affected 67 rows; later historical facts were intentionally excluded. | Preserve snapshot/report; assess later rows only under normal semantic review. |
| Medium | **2b is separate:** senior-ai-eng’s 73 semantic-edge closures are not ADR-0015 progress. JobLab independently shows semantic over-claims. | Make provenance-backed semantic-edge review a post-Tier-2, per-root quality gate. |
| Medium | Temporal source semantics are designed but unimplemented. | Implement [ADR-0019](../docs/adr/ADR-0019-temporal-source-semantics.md) later as its non-destructive, snapshot-default migration. |
| High (DOCUMENTED) | SEC-01 AWS credential scope was administrator-level; not re-verified here. | Owner should replace it with a least-privilege Bedrock principal. [Source](known-limitations.md). |

## 4. Tests Performed

| Command/check | Environment | Result/evidence |
| --- | --- | --- |
| `aimemory-ingest rebuild-graph` twice | Live Docker ingestion/PostgreSQL/Neo4j | **MEASURED:** identical 6,826-node / 12,398-relationship writer totals; see §3 caveat. |
| Cypher counts | `ai-memory-neo4j-1` | **MEASURED:** 7,251 nodes / 13,150 relationships. |
| PostgreSQL canonical counts | `ai-memory-postgres-1` | **MEASURED:** 1,160 sources; 1,885 entities; 2,083 facts; 3,397 artifacts; 997 episodes; facts 1,988 current / 87 historical / 8 unconfirmed. |
| `pytest tests/unit/test_contracts_ontology.py tests/integration/test_contracts_functional_index.py -q` | Docker tools | **MEASURED:** 25 passed. |
| Small validation extraction with `LLM_PROVIDER=luna` | Live ingestion, registered model | **MEASURED:** 4 persisted facts; none in the three ADR-0015 target predicates. Small sample only. |
| JobLab DE Tier 2 | Live `joblab-de`, equivalent Haiku route | **MEASURED:** 60 extracted / 1 failed; 0/203 fact and 0/1,216 artifact provenance gaps. [Pilot report](2026-09-22-joblab-de-pilot-quality-report.md). |
| Paired evaluation harness | Live populated Neo4j | **MEASURED:** 24/25 questions scored; zero HTTP failures. See §5. |
| Refused MCP write before/after restart | Live MCP/API/PostgreSQL Docker stack | **MEASURED:** two persisted audit rows; sink gateway, no drops. See §7. |

## 5. Retrieval Benchmark

Both runs were measured today against populated Neo4j. The historical 67% report is not a before-graph baseline because it used expansion while Neo4j was empty.

| Metric | `--no-expand` | expansion | Change |
| --- | ---: | ---: | ---: |
| Expected-source hit@5 (n=15) | **MEASURED:** 20.0% (3/15) | **MEASURED:** 26.7% (4/15) | +6.7 pp |
| Expected-entity presence (n=11) | **MEASURED:** 84.8% | **MEASURED:** 93.9% | +9.1 pp |
| Provenance completeness (n=24) | **MEASURED:** 100.0% | **MEASURED:** 100.0% | unchanged |
| Retrieval HTTP failures | **MEASURED:** 0 | **MEASURED:** 0 | unchanged |
| Temporal correctness (n=1) | **MEASURED:** 100.0% | **MEASURED:** 100.0% | unchanged |

Commands: `docker compose --profile tools run --rm tools python tests/evaluation/run_eval.py --no-expand`; then the same command without `--no-expand`. Evidence: [no expansion](evaluation-20260922T155622Z.md), [expansion](evaluation-20260922T155633Z.md), [interpretation](2026-09-22-graph-expansion-evaluation.md). Retain graph expansion; this sample does not justify changing embeddings or adding a reranker.

## 6. Knowledge Quality Evaluation

### 2a — original ADR-0015 category

**MEASURED: repaired.** The original cohort’s 60 wrongful closures and 32 backward ownership directions were corrected without re-extraction. Of those 92 correction events, 25 ownership rows overlap, so 67 unique rows changed. The snapshot contains 1,935 facts. The original cohort has zero remaining target closures and zero still-flippable ownership facts. The ontology/index fix prevents the prior functional-closure category; the small Luna validation extraction did not reproduce it.

This is not a blanket semantic-quality certification: later historical rows were deliberately not touched.

### 2b — semantic-edge review

**Recommendation: generalize.** The 73 senior-ai-eng closures were a separate cleanup for source-mention/dependency semantics, not the 92. JobLab’s valid-direction/endpoints but semantically over-strong edges independently demonstrate the need. Add a sampled, provenance-backed post-Tier-2 review per project root, prioritizing `DEPENDS_ON`, `DEPLOYED_ON`, `USES_ARCHITECTURE`, and ownership. It is recommended, not implemented.

## 7. MCP Audit Verification

**MEASURED: PASS.** With live writes enabled, deliberately refused `memory.add_episode` calls (`confirm=false`) were made before and after an announced `docker restart ai-memory-memory-api-1`.

- Before restart: **`92fd764d-2bb4-4a40-b8f5-aefacedaa697`** — `allowed=false`, `denied_reason=confirm_required`.
- After restart and healthy Memory API: **`0f3eddf3-8e21-45d4-ab7b-aa99b5c6f048`** — same refused-write evidence.

Direct PostgreSQL verified both `mcp_audit_log` rows. MCP `/health` afterward was `sink=gateway`, `degraded=false`, `submitted=4`, `persisted=4`, `dropped=0`. This proves a post-restart write is audited; it does not claim a write was submitted during the outage. [Exact commands and timestamps](2026-09-22-mcp-audit-e2e-verification.md).

## 8. Recommended Next Development Phase

### Immediate

1. Keep broader ingestion paused; repair/retry JobLab’s failure and complete semantic-edge/entity review.
2. Address SEC-02 separately; owner should also resolve DOCUMENTED SEC-01 IAM scope.
3. Reconcile the rebuild relationship-counter discrepancy.

### Future

1. Implement ADR-0019 with its tests and `snapshot` default.
2. Make semantic-edge review a repeatable per-root quality gate.
3. Improve retrieval only after a larger cleaned gold set clarifies corpus noise and abstention behavior.

### Optional

1. Add an operator reconciliation view for PostgreSQL, Neo4j, and writer totals.
2. Expand evaluation after JobLab passes its quality gate.
3. Revisit object storage only near the documented ~500 MB MIRROR threshold.
