# Ontology and Neo4j Projection (V0.1)

Status: **implementable spec**, frozen with the P1 contract · Author: A02 · Date: 2026-09-14
Revised 2026-09-17 for **ADR-0015** (predicate cardinality and direction); ontology version `0.2.0`
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

The **Functional predicates** column is not decoration: it *is* the subject list of each functional
predicate (`Ontology.functional_subjects`), and the loader rejects a node type that claims a
predicate the `functional_predicates` block does not define.

| Label | Meaning | Functional predicates |
|---|---|---|
| `Project` | top-level initiative with an output and a goal | `HAS_STATUS`, `HAS_STAGE`, `SELECTED_OPTION` |
| `SubProject` → stored as `Project` | a Project with `parent_id` | `HAS_STATUS`, `HAS_STAGE`, `SELECTED_OPTION` |
| `Person` | a human (the owner or a collaborator) | |
| `Organization` | company, university, community | |
| `Technology` | library, framework, platform, language, service (alias seed: `config/technology-aliases.yaml`) | |
| `Concept` | method, pattern, idea | `HAS_STAGE` |
| `Document` | an INDEX_CONTENT source rendered as a node | `HAS_STATUS` |
| `Repository` | a git repository root | `HAS_STATUS` |
| `Source` | any registered source; mirrors `sources` | `HAS_STATUS` |
| `Device` | logical machine holding sources | |
| `Decision` | a choice made, with status and validity | `HAS_STATUS`, `SELECTED_OPTION` |
| `Requirement` | something the project must satisfy | `HAS_STATUS`, `SELECTED_OPTION` |
| `Task` | a unit of work with a status | `HAS_STATUS`, `HAS_STAGE`, `SELECTED_OPTION` |
| `Experiment` | a trial with a hypothesis and an outcome | `HAS_STATUS`, `HAS_STAGE`, `SELECTED_OPTION` |
| `Dataset` | data collection used or produced | `HAS_STATUS`, `HAS_STAGE` |
| `ResearchFinding` | an evidence-backed result | `HAS_STATUS` |
| `Episode` | a unit of memory change | |
| `Application` | a deployed/deployable software product | `HAS_STATUS`, `HAS_STAGE` |
| `InfrastructureComponent` | server, cloud resource, container, network piece | `HAS_STATUS` |

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

22 types in three groups. Group membership decides who may write them.

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
| `HAS_OWNER` | `*` | Person, Organization |
| `USES_ARCHITECTURE` | Project, SubProject, Application, Repository, InfrastructureComponent, Technology, Dataset, Experiment | Technology, Concept, InfrastructureComponent |
| `DEPLOYED_ON` | Application, Repository, Project, SubProject, InfrastructureComponent, Technology, Dataset | InfrastructureComponent, Technology, Organization, Device |

The last three were functional predicates until ADR-0015. They are multi-valued — a project is built
on several technologies and owned by more than one person at a time — and, being attribute-like, they
had no declared endpoints at all, which is how 38 of 58 `HAS_OWNER` facts came to be written
backwards (`Person -[HAS_OWNER]-> Project`) without a single validation error.

**Temporal:**

| Type | From | To |
|---|---|---|
| `SUPERSEDES` | Decision, Requirement, Task, ResearchFinding | Decision, Requirement, Task, ResearchFinding |
| `DECIDED_IN` | Decision | Episode |

`"*"` is the wildcard (`aimemory.ontology.WILDCARD`). `Ontology.validate_relationship(predicate,
from_label, to_label)` must be called by A08 before every edge write; an out-of-ontology edge raises
`OntologyError` and the fact is dropped with a recorded reason rather than written.

### Direction is part of the contract (ADR-0015)

`from`/`to` are a **direction**, not a set of participating labels. `Project -[HAS_OWNER]-> Person`
and `Person -[HAS_OWNER]-> Project` state different things, and only the first is the ontology's.

Before writing a fact whose object is a resolved entity, A08 calls `Ontology.orient(predicate,
from_label, to_label) -> Orientation` instead of `validate_relationship`. There are exactly three
outcomes:

| Declared direction legal? | Reversed legal? | Result |
|---|---|---|
| yes | — | returned unchanged, `flipped=False` |
| no | yes | returned reversed, `flipped=True`, `reason` filled in |
| no | no | `OntologyError`; the fact is dropped with a recorded reason |
| yes | yes | returned unchanged (`Person -[HAS_OWNER]-> Person` is genuinely ambiguous) |

A flip is only ever applied when it is *unambiguous* — illegal one way, legal the other. Ambiguity is
never resolved by guessing, and `flipped` must be persisted with the fact so `explain` can show that
the system, not the document, chose the direction. A literal object is never flipped: a value cannot
become a subject.

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

A functional predicate admits **one current object per subject**, so a new value closes the previous
one (`valid_to = new.valid_from`, `status = historical`) — ADR-0005 rule 1. `is_functional(predicate)`
is the single switch that drives it; see `temporal.md`.

### The membership test

> A predicate is functional only when **two concurrent values would be a contradiction**, not merely
> unusual.

This is the rule ADR-0015 added, and it is the whole of the decision. Getting it wrong in the
permissive direction is cheap and visible: two current values coexist, the stale one lingers, a
reader sees both. Getting it wrong in the restrictive direction is expensive and invisible: a true,
current fact is rewritten as `historical` and silently disappears from every answer. MEASURED on the
2026-09-17 vault corpus, `JobLab USES_ARCHITECTURE` held 5 current and 13 historical objects, with
`Python` and `PostgreSQL` — both concurrently true — filed as history.

```yaml
functional_predicates:
  HAS_STATUS:      {to: [literal]}          # a thing has one status; two is a contradiction
  HAS_STAGE:       {to: [literal, Concept]} # one stage at a time; moving on supersedes
  SELECTED_OPTION: {to: ["*", literal]}     # one option chosen at a time
```

These are **not** relationship types, so they declare no `from` list: their legal subjects are
exactly the node types that name them in `node_types.<T>.functional` (§2's table), and the loader
enforces the correspondence in both directions. `to` declares the object kind — `literal` means a
value in `facts.object_value`, a label means a resolved entity, `"*"` means any entity. Passing
`to_label=None` to `validate_relationship` asserts a literal object.

In PostgreSQL they are ordinary `facts` rows; in Neo4j they appear as a node property (`status`) or,
when the object is an entity, as a `RELATED_TO` edge tagged with the real predicate.

`HAS_STATUS` takes `literal` only. `Mohammad HAS_STATUS "Picnic"` — 7 of 7 `HAS_STATUS` facts in the
corpus, MEASURED — is a job application mis-typed as a status; it is now rejected at the boundary
rather than stored and then answered.

### Everything else accumulates

Non-functional predicates accumulate: `Project USES Postgres` and `Project USES Neo4j` are both
current at the same time; only a contradiction or an explicit supersession closes one. That includes
`HAS_OWNER`, `USES_ARCHITECTURE` and `DEPLOYED_ON` since ADR-0015.

Supersession for a multi-valued predicate is not lost, it just comes from a different rule: ADR-0005
rule 2 (the text says "we migrated from Snowflake to Databricks", or `record_decision(supersedes=…)`)
and rule 3 (a value the new source version no longer states becomes `unconfirmed`). A genuine
architecture migration is best modelled as a `Decision` whose `SELECTED_OPTION` changed — which *is*
functional, and supersedes exactly as intended.

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
5. **ADR-0015: `HAS_OWNER`, `USES_ARCHITECTURE` and `DEPLOYED_ON` are no longer functional** and are
   now declared semantic relationship types (19 → 22 edge types, 6 → 3 functional predicates). The
   plan's section H listed all six as single-valued; MEASURED evidence from the vault corpus showed
   three of them collapsing concurrently-true values into `historical`.
6. **ADR-0015: functional predicates are endpoint-checked like every other predicate.** Until 0.2.0
   `validate_relationship` returned early for them ("endpoints are not constrained"), so nothing
   could catch a reversed subject/object pair. Their subjects now come from `node_types.<T>.functional`
   and their object kind from `to`, and `Ontology.orient()` repairs an unambiguously reversed triple.
7. **`literal` sentinel added to the `to` vocabulary** (alongside `"*"`), so "this predicate's object
   is a value, not an entity" is machine-checkable. Lower-case, so it cannot collide with a label.

## Related

`data-model.md` (the rows behind each node) · `temporal.md` (validity and supersession) ·
`retrieval.md` (how expansion uses these edges) · ADR-0001, ADR-0002, ADR-0005, ADR-0006, ADR-0015.
