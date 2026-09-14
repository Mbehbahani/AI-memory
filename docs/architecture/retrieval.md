# Hybrid Retrieval and Context Assembly (V0.1)

Status: **implementable spec**, frozen with the P1 contract · Author: A02 · Date: 2026-09-14
Authority: plan sections P and Q · Configuration: `config/retrieval.yaml` ·
Consumer: **A09** (P9-T01, P10-T01/T02), with A10 (MCP `memory.search`), A12 (gold-set evaluation),
A16 (Ops metrics). Code contracts: `aimemory.domain.retrieval`.

Every stage below maps to one type in `aimemory/domain/retrieval.py`; A09 implements functions
between those types and nothing else invents a shape.

```text
SearchQuery
  ├─ 1. semantic candidates  ──► Candidate[]   (pgvector HNSW, semantic_top_k)
  ├─ 2. keyword candidates   ──► Candidate[]   (tsvector,      keyword_top_k)
  ├─ 3. fusion (RRF, rrf_k)  ──► ScoredHit[]   (fused_top_k)
  ├─ 4. graph expansion      ──► RelatedEntity[] + entity_linked boost
  ├─ 5. temporal filter      ──► ScoredHit[]   (as_of / since / status)
  ├─ 6. boosts + rank        ──► ScoredHit[]   (final_k)
  ├─ 7. provenance           ──► ScoredHit.provenance + citation
  └─ 8. context assembly     ──► AssembledContext  ──► SearchResult
                                                     └─ retrieval_logs row
```

## 0. Configuration keys

`config/retrieval.yaml` is the only tuning surface; `RetrievalConfig` is its typed view (each field's
docstring names the YAML path). A09 loads it once at startup and logs the effective values.

| YAML key | Field | V0.1 value |
|---|---|---|
| `candidates.semantic_top_k` | `semantic_top_k` | 40 |
| `candidates.keyword_top_k` | `keyword_top_k` | 40 |
| `candidates.fused_top_k` | `fused_top_k` | 15 |
| `candidates.final_k` | `final_k` | 10 |
| `fusion.method` | `fusion_method` | `rrf` |
| `fusion.rrf_k` | `rrf_k` | 60 |
| `boosts.project_match` | `boost_project_match` | +0.10 |
| `boosts.entity_linked` | `boost_entity_linked` | +0.10 |
| `boosts.unconfirmed_penalty` | `unconfirmed_penalty` | −0.15 |
| `boosts.recency_half_life_days` | `recency_half_life_days` | 180 |
| `graph_expansion.enabled` | `graph_expansion_enabled` | true |
| `graph_expansion.max_depth` | `graph_max_depth` | 1 |
| `graph_expansion.max_nodes` | `graph_max_nodes` | 25 |
| `graph_expansion.relationship_types` | `graph_relationship_types` | 11 ontology predicates |
| `context.token_budget` | `token_budget` | 6000 |
| `context.block_order` | `block_order` | `[project_summary, current_facts, decisions, evidence_chunks, related_entities]` |
| `context.citation_format` | `citation_format` | `[{source_uri}#{heading} @{hash8}]` |

`tests/unit/test_contracts.py::test_retrieval_config_model_covers_config_file` asserts that every key
above exists and that each `graph_expansion.relationship_types` entry is a real ontology predicate.

## 1. Semantic candidates

Embed the query with the same model as the corpus (`EmbeddingProvider.embed_query`, MiniLM-L6-v2,
384-d, normalized), then:

```sql
SELECT e.object_type, e.object_id, 1 - (e.vector <=> :q) AS raw_score
FROM embeddings e
JOIN chunks c        ON e.object_type = 'chunk'    AND c.id = e.object_id
JOIN sources s       ON s.id = c.source_id
WHERE e.model_id = :embedding_model_id
  AND (:project_ids IS NULL OR c.project_id = ANY(:project_ids))
  AND s.status = ANY(:allowed_source_status)     -- 'active' unless include_deleted_sources
  AND s.policy IN ('INDEX_CONTENT','MIRROR')     -- CATALOG_ONLY has no text to cite
  AND s.secret_suspected = false                 -- flagged content is never returned
  AND (:since IS NULL OR c.created_at >= :since)
ORDER BY e.vector <=> :q
LIMIT :semantic_top_k;
```

A second, identically-shaped query runs against artifacts (`e.object_type = 'artifact'` joined to
`knowledge_artifacts`), applying the temporal predicate of section 5 in the `WHERE` clause.

Notes A09 must respect:

* the filters are **inside** the SQL, not applied afterwards — filtering after `LIMIT 40` silently
  empties a project-scoped query;
* `SET LOCAL hnsw.ef_search = 80` for the statement (recall over latency at this corpus size);
* the operator is `<=>` (cosine) matching `vector_cosine_ops` in the index.

## 2. Keyword candidates

```sql
SELECT 'chunk', c.id, ts_rank_cd(c.tsv, q) AS raw_score
FROM chunks c, plainto_tsquery('english', :query) q
JOIN sources s ON s.id = c.source_id
WHERE c.tsv @@ q
  AND <the same scoping filters as above>
ORDER BY raw_score DESC
LIMIT :keyword_top_k;
```

Artifacts use the `to_tsvector('english', title || ' ' || body)` expression index. Keyword search is
what makes exact identifiers (`ADR-0007`, `pgvector`, `joblab-de`) findable when the embedding is
too diffuse; it is never skipped.

## 3. Reciprocal Rank Fusion

```text
FOR each retriever r IN (semantic, keyword):
    FOR each candidate c at 1-based rank i in r:
        rrf[key(c)] += 1 / (rrf_k + i)          # rrf_k = 60
```

`key(c)` is `(object_type, object_id)`. The RRF score is stored in `ScoredHit.rrf_score` and the set
of contributing retrievers in `ScoredHit.retrievers`, so the Ops page can show how a hit was found.
Take the top `fused_top_k` (15) into the next stage.

RRF is chosen because raw cosine similarity and `ts_rank_cd` are not comparable; rank fusion needs no
score normalization and no training data.

## 4. Graph expansion

Only when `expand = true` and `graph_expansion.enabled`:

1. collect the entity ids of the fused hits — for a chunk via `entity_mentions.chunk_id`, for an
   artifact via `artifact_entities`;
2. one Cypher call, bounded by `max_depth` (1) and `max_nodes` (25), restricted to
   `graph_expansion.relationship_types`, with the temporal predicate of section 5 applied to the edge:

```cypher
MATCH (n:Entity)-[r]-(m:Entity)
WHERE n.id IN $entity_ids
  AND type(r) IN $relationship_types
  AND r.valid_from <= datetime($as_of)
  AND (r.valid_to IS NULL OR r.valid_to > datetime($as_of))
  AND ($project_ids IS NULL OR m.project_id IN $project_ids)
RETURN m.id, m.name, m.type, type(r) AS predicate, r.fact_id, r.confidence,
       r.valid_from, r.valid_to, startNode(r).id = n.id AS outgoing
LIMIT $max_nodes;
```

3. the neighbours become `RelatedEntity[]`; any fused hit linked to one of them earns the
   `entity_linked` boost.

**Degraded mode is mandatory.** If Neo4j is unreachable, A09 logs it, appends
`"graph expansion unavailable; vector+keyword only"` to `SearchResult.warnings`, and continues. A
failed graph call never fails a query (plan section Y failure test "Neo4j down → vector-only with
warning").

## 5. Temporal filter

The ADR-0005 predicate, applied to every fact and artifact (chunks are immutable text and are
filtered by their source's status instead):

```sql
AND a.valid_from <= :as_of AND (a.valid_to IS NULL OR a.valid_to > :as_of)
```

`as_of` defaults to `now()`. `since` filters `observed_at` / version time — "what changed lately" —
and is deliberately a different axis (see `temporal.md` §8).

`status = 'unconfirmed'` rows are **kept** (ADR-0005 rule 3) and penalised in the next stage;
`include_unconfirmed = false` drops them entirely for callers who want only re-confirmed knowledge.

## 6. Boosts and final ranking

```text
score = rrf_score
      + (project_match     if hit.project_id ∈ query.project_ids)        # +0.10
      + (entity_linked     if hit shares an entity with the expansion)   # +0.10
      + recency_boost(hit)                                              # see below
      + (unconfirmed_penalty if hit.status == 'unconfirmed')            # -0.15

recency_boost(hit) = 0.10 * 0.5 ** (age_days(hit.observed_at) / recency_half_life_days)
```

Every term is recorded separately in `ScoredHit.boosts` — a ranking change must be explainable
without re-running the query. Sort by `score` descending, assign `rank` from 1, keep `final_k` (10).

Ties break by `observed_at` descending, then by `object_id`, so results are deterministic and the
gold-set evaluation is reproducible.

Additional, non-configurable penalty: sources with `trust = 'low'` (AC-6, vault `Clippings/`) receive
the same magnitude as `unconfirmed_penalty`. This is recorded in `boosts["low_trust"]`.

## 7. Provenance attachment

For every returned hit A09 fills `ScoredHit.provenance` from `provenance_v` (one query with
`object_id = ANY(:ids)`) and renders `ScoredHit.citation` with `context.citation_format`, using
`aimemory.common.hashing.short_hash` for `@{hash8}`.

Acceptance (plan section Y): **provenance completeness is 100 %** on returned evidence. A hit whose
provenance cannot be resolved is dropped and a warning is added — the system never cites something it
cannot trace.

The Gateway returns the *logical* source URI and, only if the root is currently mounted, the mapped
container path. It **never returns file bytes** (plan section Q).

## 8. Context assembly

Blocks are built in `context.block_order` and each is budgeted:

| Block | Content | Typical share of 6000 tokens |
|---|---|---|
| `project_summary` | project name, track, status, coverage note | ~150 |
| `current_facts` | current facts for the scoped entities, oldest-stable first, each cited | ~900 |
| `decisions` | current decision artifacts (+ "supersedes …" line) | ~900 |
| `evidence_chunks` | the top hits' text, in rank order | remainder |
| `related_entities` | names + predicates from expansion | ~300 |

Algorithm:

```text
budget := context.token_budget                    # 6000
FOR kind IN context.block_order:
    block := build(kind)
    IF block.tokens <= budget:
        emit(block); budget -= block.tokens
    ELSE IF kind == 'evidence_chunks':
        emit(truncate_to(block, budget)); budget := 0; truncated := true
    ELSE:
        dropped_blocks.append(kind); truncated := true
```

`evidence_chunks` is the only block that may be partially included; the others are all-or-nothing so
a decision is never shown half-stated. `AssembledContext.truncated` and `dropped_blocks` are returned
to the caller, and `AssembledContext` refuses to validate if `tokens_used > token_budget`.

Every block carries its own `citations`, and `flags` marks `unconfirmed`, `source-deleted` or
`low-trust` content inline, so a reader of the context sees the caveat next to the claim.

Token counting in V0.1 is a MEASURED-at-runtime approximation
(`ceil(len(text) / 4)` characters-per-token), consistent between assembly and budgeting. It is a
budget guard, not a billing number, and is labelled ESTIMATED wherever it is reported.

## 9. Logging

One `retrieval_logs` row per query: text, hash, scoping, `as_of`/`since`, effective `params`,
`candidate_counts` per retriever, the returned `result_ids`, `latency_ms`, `client` and `warnings`.
This row is what makes the ADR-0010 gold-set comparison possible across model and config changes.

## 10. Gateway surface (plan section Q)

`packages/aimemory/gateway/` holds the service functions; `apps/memory-api` is a thin REST layer.

| Route | Returns | Notes |
|---|---|---|
| `POST /v1/search` | `SearchResult` | the pipeline above |
| `GET /v1/projects`, `/v1/projects/{id}` | project registry + coverage | |
| `GET /v1/entities/{id}` | entity + current facts + provenance | |
| `GET /v1/related` | `RelatedEntity[]` | graph expansion only |
| `GET /v1/decisions` | decision artifacts | `include_superseded` |
| `GET /v1/timeline` | merged temporal events | see `temporal.md` §8 |
| `GET /v1/sources` | source registry (URIs, never bytes) | |
| `GET /v1/artifacts/{id}` | artifact + supersession chain | |
| `GET /v1/state` | current state + honest coverage | ADR-0006 |
| `GET /v1/explain/{id}` | `ExplainChain` | plan section J |
| `POST /v1/episodes`, `POST /v1/decisions` | write paths | 403 unless `GATEWAY_WRITE_ENABLED` |
| `GET /health`, `/metrics`, `/ops` | liveness, counters, Ops page (ADR-0011) | |

Access boundaries: memory-api connects to Neo4j as `NEO4J_READONLY_USER`; writes are refused with
`WriteDisabledError` unless the flag is on; MCP adds its own `confirm`, rate limit and audit
(ADR-0008).

## Deviations

1. **`recency_boost` is given an explicit formula.** The plan says "recency decay" with a half-life
   but no shape; exponential decay capped at +0.10 keeps it comparable in magnitude to the other
   boosts.
2. **A `low_trust` penalty is specified.** AC-6 says clippings rank lower but no mechanism was
   defined; it reuses the `unconfirmed_penalty` magnitude and is reported separately in `boosts`.
3. **Deterministic tie-breaking is required** (`observed_at` desc, then `object_id`). Without it the
   gold-set numbers wobble between runs and a regression cannot be distinguished from noise.
4. **Only `evidence_chunks` may be truncated**; other blocks are dropped whole. The plan gives a
   budget but not a truncation policy, and a half-quoted decision is worse than an omitted one.
5. **Token counting is `len/4`.** No tokenizer is available in the Gateway container (the LLM lives
   in Ollama); the estimate is consistent and labelled.
6. **`hnsw.ef_search = 80` is set per statement.** Not mentioned in the plan; without it the default
   recall is noticeably lower on a small corpus.
7. **Secret-suspected and CATALOG_ONLY sources are excluded in SQL**, not by post-filtering, making
   plan section T's "flagged files never stored or logged" verifiable at the query level.

## Related

`data-model.md` (the indexes these queries rely on) · `ontology.md` §3 (expansion edge types) ·
`temporal.md` (the `as_of` predicate and `unconfirmed`) · `config/retrieval.yaml` ·
ADR-0005, ADR-0006, ADR-0007, ADR-0008, ADR-0010.
