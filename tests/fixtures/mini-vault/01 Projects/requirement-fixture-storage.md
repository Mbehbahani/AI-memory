---
title: Requirement - durable storage
project: fixture-project
status: current
tags: [requirement]
---

# Requirement: durable storage

[[Fixture Project]] must keep its system of record in PostgreSQL, with Neo4j as a rebuildable
projection (see [[architecture-decision-b]]). This mirrors ADR-0001 of the real project for fixture
purposes only.

## Acceptance

- Neo4j can be dropped and rebuilt from PostgreSQL without data loss.
- No fact is ever deleted, only closed (`valid_to`).
