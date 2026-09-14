# Contracts

Machine-readable contracts shared by every agent and service. Owner: A02 (Architecture & Contracts).
Changing anything here after P1 requires an ADR and a note in `reports/task-ledger.md`.

| File | Consumers |
|---|---|
| `ontology.yaml` | ontology package, extraction prompts, Neo4j projection, Gateway, NeoDash queries |
| `extraction/episode_extraction.schema.json` | native engine call 1 (Ollama `format`), validation, tests |
| `extraction/relationship_extraction.schema.json` | native engine call 2 |
| `mcp/tools.json` | mcp-server implementation, MCP client tests, docs |
| `api/openapi.json` | generated from memory-api at build time (P10); committed for diffing |
