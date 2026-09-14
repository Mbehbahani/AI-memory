# Security

Written by A13 in P15: `threat-model.md`, `checklist-<date>.md` (10 verified items from plan §T with
commands and results), findings with severity, and entries for `reports/known-limitations.md`.

Standing rules (already in force from the scaffold): read-only source mounts, explicit roots only,
loopback-only published ports (ADR-0007), `.env` never committed, built-in deny list + `.memoryignore`
+ secret detector (`config/policies.yaml`), Neo4j read-only user for readers, MCP writes off by default
(ADR-0008), no file bytes through the Gateway, sanitized errors, append-only audit tables.
