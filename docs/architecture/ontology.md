# Ontology and Neo4j Projection (V0.1)

Status: **implementable spec**, frozen with the P1 contract · Author: A02 · Date: 2026-09-14
Contract file: `schemas/ontology.yaml` · Loader: `packages/aimemory/ontology/loader.py`
Consumers: **A04** (constraints, P5-T02), **A08** (projection, entity resolution, temporal edges),
**A09** (graph expansion), **A11** (NeoDash queries), A05 (prompt vocabularies), A12 (tests).

Neo4j is a **rebuildable projection** (ADR-0001). No fact exists only in the graph;
`scripts/rebuild-graph` replays the whole projection from PostgreSQL. Nothing in this document may
require reading from Neo4j to answer a question that PostgreSQL can answer.

## 1. Two population layers

| Layer | Written by | When | `engine` property |
|---|---|---|---|
| **Structural** | deterministic projection from registries and document structure | after Tier 0/1, no LLM | `deterministic` |
| **Semantic** | the `KnowledgeEngine` chosen by the ADR-0002 gate | Tier 2 | `native` or `graphiti` |

Both write the same labels, the same property names and the same edge types. The Gateway cannot tell
them apart except by reading `engine`, which is exactly the point of ADR-0002.

## 2. Node types

19 entries in `node_types`, **18 distinct Neo4j labels**. `SubProject` carries
`stored_as: Project`: a sub-project is written as `:Project` with `parent_id` set, not as its own
label. `Ontology.stored_label()` performs the mapping and
`tests/unit/test_contracts_ontology.py::test_stored_labels_match_the_neo4j_constraints_file`
asserts that `infra/neo4j/schema/constraints.cypher` declares one uniqueness constraint per stored
label.

| Label | Meaning | Functional predicates |
|---|---|---|
| `Project` | top-level initiative with an output and a goal | `HAS_STATUS`, `HAS_OWNER` |
| `SubProject` → stored as `Project` | a Project with `parent_id` | `HAS_STATUS` |
| `Person` | a human (the owner or a collaborator) | |
| `Organization` | company, university, community | |
| `Technology` | library, framework, platform, language, service (alias seed: `config/technology-aliases.yaml`) | |
| `Concept` | method, pattern, idea | |
| `Document` | an INDEX_CONTENT source rendered as a node | |
| `Repository` | a git repository root | |
| `Source` | any registered source; mirrors `sources` | |
| `Device` | logical machine holding sources | |
| `Decision` | a choice made, with status and validity | `HAS_STATUS` |
| `Requirement` | something the project must satisfy | `HAS_STATUS` |
| `Task` | a unit of work with a status | `HAS_STATUS` |
| `Experiment` | a trial with a hypothesis and an outcome | |
| `Dataset` | data collection used or produced | |
| `ResearchFinding` | an evidence-backed result | |
| `Episode` | a unit of memory change | |
| `Application` | a deployed/deployable software product | |
| `InfrastructureComponent` | server, cloud resource, container, network piece | |

`Source`, `Device` and `Episode` are **never** proposed by the extraction model
(`EXTRACTABLE_ENTITY_TYPES`): they come from the registry and the ingestion run, so a hallucinated
one would create unverifiable provenance.

### Node properties

`schemas/ontology.yaml: node_properties` is machine-readable and enforced by
`PropertyContract.missing()`:

```yaml
node_properties:
  required: [id, name, type, observed_at, engine]
  optional: [project_id, summary, status, valid_from, valid_to, source_id, episode_id, confidence]
```

| Property | Type in Neo4j | Source |
|---|---|---|
| `id` | string | `entities.id` (or `projects.id` / `sources.id` / `devices.id` for registry nodes) — **the PostgreSQL id**, which is what makes the projection replayable |
| `name` | string | `entities.canonical_name` |
| `type` | string | the ontology type, including `SubProject` when the stored label is `Project` |
| `project_id` | string | scoping key used by every dashboard and expansion query |
| `summary` | string | |
| `status` | string | |
| `valid_from`, `valid_to`, `observed_at` | datetime | ISO-8601 UTC; `valid_to` absent/`null` = current |
| `source_id`, `episode_id` | string | provenance back-pointers |
| `confidence` | float | |
| `engine` | string | `deterministic` \| `native` \| `graphiti` |
| `parent_id` | string | **only** on `:Project` nodes that represent a `SubProject` |

### Write convention

```cypher
MERGE (n:Project {id: $id})
SET   n += $props,        // props always contains the required set above
      n.updated_at = datetime()
```

Always `MERGE` on `(label, id)`, never `CREATE`; `id` comes from PostgreSQL, so a replay of the same
episode converges instead of duplicating. Labels are never parameterised in Cypher (Neo4j forbids
it): A08 builds one statement per label from `Ontology.stored_labels` and validates the label against
the ontology *before* interpolating it.

## 3. Relationship types

19 types in three groups. Group membership decides who may write them.

**Structural (deterministic, no LLM):**

| Type | From | To |
|---|---|---|
| `PART_OF` | SubProject, Document, Task, Requirement | Project, SubProject |
| `BELONGS_TO` | Document, Repository, Source, Dataset, Experiment, Decision | Project, SubProject |
| `STORED_ON` | Source, Repository | Device |
| `HAS_SOURCE` | Document, Decision, Requirement, Task, ResearchFinding, Episode | Source |
| `MENTIONS` | Document, Episode | `*` |
| `LINKS_TO` | Document | Document |
| `DERIVED_FROM` | Decision, Requirement, Task, ResearchFinding, Concept | Episode |

**Semantic (LLM-extracted, carry `confidence`):**

| Type | From | To |
|---|---|---|
| `USES` | Project, SubProject, Application, Experiment, Repository | Technology, Dataset, Concept |
| `DEPENDS_ON` | Project, SubProject, Application, InfrastructureComponent, Task | `*` |
| `RELATED_TO` | `*` | `*` |
| `IMPLEMENTS` | Repository, Application, Project | Concept, Requirement, Decision |
| `SUPPORTS` | ResearchFinding, Experiment, Document | Decision, Concept, Requirement |
| `CONTRADICTS` | ResearchFinding, Experiment, Document, Decision | Decision, ResearchFinding, Concept |
| `PRODUCES` | Project, Experiment, Task | Dataset, Document, Application, ResearchFinding |
| `REQUIRES` | Project, Task, Requirement, Decision | Technology, Requirement, Task |
| `CREATED_BY` | `*` | Person, Organization |
| `GENERATED_BY` | Document, Dataset, ResearchFinding | Experiment, Task, Application |

**Temporal:**

| Type | From | To |
|---|---|---|
| `SUPERSEDES` | Decision, Requirement, Task, ResearchFinding | Decision, Requirement, Task, ResearchFinding |
| `DECIDED_IN` | Decision | Episode |

`"*"` is the wildcard (`aimemory.ontology.WILDCARD`). `Ontology.validate_relationship(predicate,
from_label, to_label)` must be called by A08 before every edge write; an out-of-ontology edge raises
`OntologyError` and the fact is dropped with a recorded reason rather than written.

### Relationship properties

```yaml
relationship_properties:
  required: [fact_id, valid_from, observed_at, engine]
  optional: [valid_to, confidence, episode_id, source_id]
```

`fact_id` is `facts.id` and is the MERGE key:

```cypher
MATCH (a {id: $from_id}), (b {id: $to_id})
MERGE (a)-[r:USES {fact_id: $fact_id}]->(b)
SET   r += $props
```

Closing an edge never deletes it (ADR-0005):

```cypher
MATCH ()-[r {fact_id: $fact_id}]->()
SET r.valid_to = datetime($at)
```

## 4. Functional predicates

```yaml
functional_predicates: [HAS_STATUS, HAS_OWNER, USES_ARCHITECTURE, DEPLOYED_ON, HAS_STAGE, SELECTED_OPTION]
```

These are **not** relationship types. Their object is normally a literal
(`facts.object_value`), so in PostgreSQL they are ordinary `facts` rows and in Neo4j they appear as a
node property (`status`) or as a `RELATED_TO` edge only when the object is itself an entity.
`is_functional(predicate)` is the single switch that drives ADR-0005 rule 1 — see `temporal.md`.

Non-functional predicates accumulate: `Project USES Postgres` and `Project USES Neo4j` are both
current at the same time; only a contradiction or an explicit supersession closes one.

## 5. Constraints and indexes

`infra/neo4j/schema/constraints.cypher` (owner A04, applied by `aimemory-ingest migrate`,
idempotent via `IF NOT EXISTS`):

* one `CREATE CONSTRAINT <label>_id ... REQUIRE n.id IS UNIQUE` for each of the **18 stored labels**;
* `CREATE INDEX entity_name FOR (n:Entity) ON (n.name)` and the `project_id` / `valid_to` indexes;
* `CREATE FULLTEXT INDEX entity_fulltext FOR (n:Entity) ON EACH [n.name, n.summary]`.

`:Entity` is a **secondary label** carried by every projected knowledge node in addition to its
ontology label, so that the name/project/validity indexes and the fulltext index exist once instead
of eighteen times. Registry-only nodes (`:Device`, `:Source`) do not carry it. A08 writes
`MERGE (n:Technology:Entity {id: $id})`.

## 6. Structural projection (Tier 0/1, deterministic)

Produced by A08's `knowledge/structural.py` directly from PostgreSQL, with
`engine = "deterministic"` and `extraction_model_id = "deterministic:registry-v1"`:

| PostgreSQL row | Neo4j |
|---|---|
| `projects` | `(:Project:Entity {id, name, type:'Project'\|'SubProject', project_id:id, status, parent_id})` |
| `projects.parent_id` | `(:Project)-[:PART_OF {engine:'deterministic'}]->(:Project)` |
| `devices` | `(:Device {id, name})` |
| `source_roots` + `sources` | `(:Source {id, name:relative_path, ...})-[:STORED_ON]->(:Device)` |
| `sources` with `policy=INDEX_CONTENT` | `(:Document:Entity {id: source_id, name: title\|relative_path})-[:HAS_SOURCE]->(:Source)` |
| `sources.project_id` | `(:Document)-[:BELONGS_TO]->(:Project)` |
| `source_text.links` (wikilinks/markdown links, resolved) | `(:Document)-[:LINKS_TO]->(:Document)` |
| `episodes` | `(:Episode {id, name:title, observed_at})-[:HAS_SOURCE]->(:Source)` |
| `entity_mentions` | `(:Document\|:Episode)-[:MENTIONS {fact_id}]->(:Entity)` |

This layer alone answers "which documents belong to JobLab DE and what do they link to" after Tier 1,
which is the ADR-0006 promise that the system is useful before any LLM has run.

## 7. Semantic projection (Tier 2)

For each `ExtractionResult` A08 has already persisted to PostgreSQL:

1. resolve entity names to `entities.id` (deterministic-first: alias table → normalized name →
   trigram candidate → new entity);
2. `MERGE` one node per entity with the full required property set;
3. for each fact, `Ontology.validate_relationship(...)`, then `MERGE` the edge on `fact_id`;
4. for each artifact, `MERGE` a `:Decision`/`:Requirement`/`:Task`/`:ResearchFinding` node plus
   `DERIVED_FROM`→`:Episode`, `BELONGS_TO`→`:Project` and, for decisions, `DECIDED_IN`→`:Episode`;
5. for supersession, `MERGE` the `SUPERSEDES` edge and set `valid_to` on the closed edge.

The order matters: **PostgreSQL first, always**. If the Neo4j write fails, the run records a
`project_graph` job failure and the graph is rebuilt later; no knowledge is lost (ADR-0001).

## 8. Vocabularies

```yaml
artifact_types:  [decision, requirement, task, finding, hypothesis, experiment, summary]
artifact_status: [current, superseded, historical, unconfirmed, proposed, done, abandoned]
episode_types:   [document, document_change, registry, manual, mcp]
tracks:          [business, research, career, foundation]
```

Each list has an exact `StrEnum` counterpart (`ArtifactType`, `ArtifactStatus`, `EpisodeType`,
`Track`) and the loader fails if they diverge. The extraction schema offers the model a *subset*:
`artifact_type` without `summary` and `status` from `StatedArtifactStatus`
(`current, proposed, done, abandoned, superseded, unknown`) — `historical` and `unconfirmed` are
conclusions the temporal engine draws, never claims the model makes.

## 9. Changing the ontology

1. Write an ADR (the contract is frozen after P1).
2. Edit `schemas/ontology.yaml`.
3. Edit the matching `StrEnum` in `aimemory/domain/enums.py` (and the extraction subsets if the model
   should be allowed to emit the new value).
4. Add the Neo4j constraint in `infra/neo4j/schema/constraints.cypher` if it is a new stored label.
5. Run `pytest tests/unit/test_contracts_ontology.py` — it fails until 2–4 agree.

## Deviations

1. **`stored_as: Project` added to `SubProject`.** The plan's section H says "18 labels" while the
   ontology file lists 19 node types; the file now states which one is not a distinct label, which
   reconciles the plan, `constraints.cypher` (18 constraints) and the code.
2. **`node_properties` / `relationship_properties` promoted from YAML comments to real keys.**
   The property contract was prose; it is now loadable and testable. `engine` and `observed_at` are
   *required* — a node without them cannot be attributed and would break `explain`.
3. **`:Entity` secondary label made explicit.** `constraints.cypher` already indexed `:Entity`
   without anything declaring who sets it; this document assigns it to every projected knowledge node.
4. **Functional predicates are documented as non-edges.** The plan lists them next to relationship
   types; they are attribute-like, live in `facts`, and only reach Neo4j as node properties.

## Related

`data-model.md` (the rows behind each node) · `temporal.md` (validity and supersession) ·
`retrieval.md` (how expansion uses these edges) · ADR-0001, ADR-0002, ADR-0005, ADR-0006.
