# Contracts

Machine-readable contracts shared by every agent and service. Owner: A02 (Architecture & Contracts).
Changing anything here after P1 requires an ADR and a note in `reports/task-ledger.md`.

| File | Consumers | Python mirror |
|---|---|---|
| `ontology.yaml` | ontology package, extraction prompts, Neo4j projection, Gateway, NeoDash queries | `aimemory.ontology.load_ontology()`, `aimemory.domain.enums.{EntityType,Predicate,ArtifactType,ArtifactStatus,EpisodeType,Track}` |
| `extraction/episode_extraction.schema.json` | native engine call 1 (Ollama `format`), validation, tests | `aimemory.domain.extraction.EpisodeExtraction` |
| `extraction/relationship_extraction.schema.json` | native engine call 2 | `aimemory.domain.extraction.RelationshipExtraction` |
| `mcp/tools.json` | mcp-server implementation, MCP client tests, docs | `aimemory.domain.retrieval` DTOs (tool outputs) |
| `api/openapi.json` | generated from memory-api at build time (P10); committed for diffing | — |

## Sync rules

* `tests/unit/test_contracts.py` asserts equivalence between every JSON Schema here and its pydantic
  mirror: property names, required lists, enums, and length/range bounds. A change to one without the
  other fails the suite.
* The extraction schemas are passed verbatim to Ollama as `format=<schema>`, so they must stay
  **flat** (no `$ref`, no `$defs`), `"type": "object"`, `additionalProperties: false`, with an
  explicit `required` list. `tests/unit/test_contracts.py::test_extraction_schemas_are_ollama_format_safe`
  enforces this.
* The extraction enums are deliberate **subsets** of the domain enums
  (`aimemory.domain.extraction.EXTRACTABLE_ENTITY_TYPES` / `EXTRACTABLE_ARTIFACT_TYPES` /
  `EXTRACTABLE_PREDICATES`): the model is never offered labels or predicates that the deterministic
  layer produces (`Source`, `Device`, `Episode`, `MENTIONS`, `HAS_SOURCE`, `LINKS_TO`, `STORED_ON`,
  `DERIVED_FROM`, `DECIDED_IN`) or a status the temporal engine alone may assign
  (`historical`, `unconfirmed`).
* `mcp/tools.json` carries a real JSON Schema per tool under `inputSchema`, so A10 can register tools
  without inventing argument shapes, and A12 can assert that the served tool list equals this file.
* URI namespaces are distinct and must not be confused: **source** URIs are `vault://`, `localfs://`
  and `git://` (ADR-0004, `aimemory.domain.source_uri`); the `memory://` URIs in `mcp/tools.json` are
  **MCP resource** URIs and address the Gateway, not the filesystem.
