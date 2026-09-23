# JobLab DE Tier-2 pilot quality report — 2026-09-22

## Scope and provider

This validation is limited to the `joblab-de` root (`joblab-lakehouse-de` project): 64 active
sources, of which 61 are `INDEX_CONTENT` and 3 are `CATALOG_ONLY`.

The corpus previously used `claude-code:haiku-4-5`. A root-scoped replay first attempted that relay
route, but its saved responses did not match the current prompt hashes and all 61 episodes became
recoverable relay failures. They were re-queued and replayed with Bedrock Claude Haiku 4.5, an
explicitly equivalent model route under `ingest_repo.EQUIVALENT_MODEL_GROUPS`; no non-equivalent
model was mixed into the corpus.

## Pipeline result

| Measure | Result |
|---|---:|
| Episodes re-queued | 61 |
| Episodes extracted | 60 |
| Episodes failed | 1 |
| Wall time | 851.4 s |
| Current facts from this root | 203 |
| Project entities | 332 |
| Project artifacts | 1,216 |

The failed source is `docs/14-phase-3-silver-relationships.md`. The failure is a persistence bug:
PostgreSQL cannot infer the type of the nullable `project_id` parameter in an artifact lookup. No
source or existing fact was deleted.

## Quality checks

### Provenance and temporal state

- Fact rows missing required provenance (`source_uri`, hash, source version, episode, extraction
  model): **0 / 203**.
- Artifact rows missing required provenance: **0 / 1,216**.
- Current facts with a reverseable `Ontology.orient()` direction: **0 / 203**.
- Current facts failing ontology endpoint validation: **0 / 203**.
- Closed facts: **0**; unconfirmed facts: **0**.

### Entity quality

All 332 project entities have a type, canonical name and normalized name. However, 36 normalized-name
duplicate groups (42 additional unmerged rows) remain, so entity resolution is not yet clean enough
to treat the entity count as a dependable canonical inventory.

### Relationship quality

The projection has valid endpoint shapes, but a manual provenance-backed sample found semantic
over-claims that schema validation cannot detect. Examples include:

- `JobLab Lakehouse (DE) DEPENDS_ON joblab-lakehouse` from wording that only calls it a counterpart.
- `JobLab DEPLOYED_ON Snowflake/Databricks` where the source describes a planned/current-state
  lakehouse component, not deployment of the broader JobLab product.
- `Snowflake trial account DEPLOYED_ON de-snowflake-loader`, reversing the operational relationship.
- `JobLab Lakehouse (DE) DEPLOYED_ON Snowflake` from text saying Gold tables are loaded there *after*
  Databricks, which is future/planned language.

The extractor also proposed invalid endpoint combinations during the pass; the ontology layer rejected
them rather than persisting them. That is a successful safeguard, but it is evidence of extraction
noise rather than a quality pass.

## Verdict

**NO-GO for wider ingestion.**

Do not expand to another project until the artifact-persistence failure is fixed and retried, and a
repeatable semantic-edge review resolves the current pilot's over-strong relationship claims and
entity duplicates. The pilot demonstrates working source-to-graph plumbing, complete provenance,
and direction/endpoint enforcement; it does not yet demonstrate sufficiently reliable semantic
knowledge quality for broad rollout.
