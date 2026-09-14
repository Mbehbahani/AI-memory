---
title: Architecture decision — Ingestion platform (revised)
project: fixture-project
tags: [decision, architecture]
date: 2026-09-11
---

# Ingestion platform: Architecture A abandoned, Architecture B selected

On 2026-09-10 **Architecture A was abandoned** because the graph could not be rebuilt after a
corruption incident. On 2026-09-11 we selected **Architecture B**: PostgreSQL as the system of record
with Neo4j as a rebuildable projection. This decision supersedes the Architecture A decision of
2026-09-01. Architecture B uses [[PostgreSQL]], [[pgvector]], and [[Neo4j]].
