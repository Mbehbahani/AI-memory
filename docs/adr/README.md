# Architecture Decision Records

Status values: proposed · accepted · superseded-by ADR-xxxx · deprecated. One decision per file.
A superseding ADR links the old one; the old one is marked superseded, never rewritten (the same rule
the memory system applies to itself). Index maintained by A01 (Build Observer).

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](ADR-0001-postgres-system-of-record.md) | PostgreSQL is the system of record; Neo4j is a rebuildable projection | accepted | 2026-09-13 |
| [0002](ADR-0002-graphiti-gate.md) | Graphiti is adopted only through a measured compatibility gate with automatic fallback | accepted | 2026-09-13 |
| [0003](ADR-0003-no-minio.md) | No object store in V0.1; MIRROR policy uses a Postgres blob table | accepted | 2026-09-13 |
| [0004](ADR-0004-source-uri-scheme.md) | Logical source URIs instead of absolute paths | accepted | 2026-09-13 |
| [0005](ADR-0005-temporal-rules.md) | Temporal memory rules: functional predicates, explicit supersession, unconfirmed-not-deleted | accepted | 2026-09-13 |
| [0006](ADR-0006-tiered-ingestion.md) | Tiered ingestion (registry → embed → LLM) with serial CPU extraction | accepted | 2026-09-13 |
| [0007](ADR-0007-loopback-exposure.md) | All published ports bind to 127.0.0.1; databases exposed only in the dev override | accepted | 2026-09-13 |
| [0008](ADR-0008-mcp-write-policy.md) | MCP writes are off by default, confirmed, audited, append-only | accepted | 2026-09-13 |
| [0009](ADR-0009-knowledge-engine-verdict.md) | Knowledge engine verdict: the native temporal engine, not Graphiti | accepted | 2026-09-15 |
| [0010](ADR-0010-evaluation-protocol.md) | Ongoing evaluation protocol and model-upgrade rule | accepted | 2026-09-14 |
| [0011](ADR-0011-ops-dashboard.md) | Built-in offline Ops dashboard and always-on ingestion worker | accepted | 2026-09-14 |
| [0012](ADR-0012-dual-llm-provider-evaluation.md) | Two extraction providers (local qwen3:4b and Bedrock Haiku 4.5) are built and compared; neither privileged. Amends B16/§T/AC-8 | accepted | 2026-09-14 |
| [0013](ADR-0013-neo4j-community-no-readonly-user.md) | Neo4j Community cannot enforce a read-only user; access constrained in the client instead. Amends §T/§Q/§S | accepted | 2026-09-14 |
| [0014](ADR-0014-extraction-provider-and-model-consistency.md) | Claude Haiku 4.5 is the default extraction provider; one model per corpus. Amends ADR-0012 §2, B16, AC-8 | accepted | 2026-09-15 |
| [0015](ADR-0015-predicate-cardinality-and-direction.md) | Predicate cardinality and direction are part of the ontology contract. Refines ADR-0005 | accepted | 2026-09-17 |
| [0016](ADR-0016-relay-provider-haiku-without-bedrock.md) | A third extraction route: `LLM_PROVIDER=relay` runs Claude Haiku 4.5 via a Claude Code subagent instead of Bedrock. Extends ADR-0012/0014 | accepted | 2026-09-19 |
| [0018](ADR-0018-luna-relay-provider.md) | `LLM_PROVIDER=luna` routes Tier 2 through the Codex GPT-5.6 Luna subagent with distinct provenance | accepted | 2026-09-21 |
| [0017](ADR-0017-keep-marked-knowledge-over-clean-loss.md) | Equivalent model routes are one model; ontology-violating edges are projected with `ontology_violation` rather than dropped. Amends ADR-0014 §2 and the ADR-0015 projection | accepted | 2026-09-19 |
| [0019](ADR-0019-temporal-source-semantics.md) | Classify sources so absence semantics are intentional, while unclassified sources retain snapshot behaviour | proposed | 2026-09-22 |
