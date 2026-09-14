# ADR-0001 — PostgreSQL is the system of record; Neo4j is a rebuildable projection

Status: accepted · Date: 2026-09-13 · Deciders: owner (Mohammad), A00

## Context
The memory needs a graph for traversal/visualization and a relational + vector store for text,
provenance, and operational state. Holding canonical facts in two places invites drift and doubles the
backup surface. Graphiti (if adopted) writes its own structures into Neo4j.

## Decision
Every entity, fact, knowledge artifact, episode, and provenance row is written to PostgreSQL first.
Neo4j holds identity, relationships, and validity windows only, projected from Postgres by a
deterministic writer. `scripts/rebuild-graph` can recreate Neo4j from Postgres at any time. If the
Graphiti engine is used, its `add_episode` results are mirrored into Postgres in the same processing
step so the invariant holds.

## Consequences
+ Backup = `pg_dump` + config. + Graphiti is replaceable. + Provenance queries are SQL.
− Two writes per fact. − Graphiti-specific Neo4j labels are regenerated only by re-running episodes
(acceptable: the Gateway never reads them directly).
