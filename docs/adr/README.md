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
| 0009 | Knowledge engine verdict (Graphiti vs native) — written by A05 after the P4 gate | pending | — |
| [0010](ADR-0010-evaluation-protocol.md) | Ongoing evaluation protocol and model-upgrade rule | accepted | 2026-09-14 |
| [0011](ADR-0011-ops-dashboard.md) | Built-in offline Ops dashboard and always-on ingestion worker | accepted | 2026-09-14 |
| [0012](ADR-0012-dual-llm-provider-evaluation.md) | Two extraction providers (local qwen3:4b and Bedrock Haiku 4.5) are built and compared; neither privileged. Amends B16/§T/AC-8 | accepted | 2026-09-14 |
| [0013](ADR-0013-neo4j-community-no-readonly-user.md) | Neo4j Community cannot enforce a read-only user; access constrained in the client instead. Amends §T/§Q/§S | accepted | 2026-09-14 |
