---
title: Architecture decision — Ingestion platform
project: fixture-project
tags: [decision, architecture]
date: 2026-09-01
---

# Ingestion platform: Architecture A selected

On 2026-09-01 we selected **Architecture A** (a single monolithic ingestion worker writing directly
to Neo4j) for the fixture project. It uses [[Neo4j]] and [[Ollama]] and depends on the
[[Embedding Service]].

Rationale: fastest path to a demo. Known risk: no system of record outside the graph.
