"""aimemory.persistence - PostgreSQL repositories (ADR-0001) and the Neo4j GraphStore projection.

Owner: A04 (P5-T01/P5-T02). See docs/architecture/data-model.md for the schema this layer talks to
and docs/architecture/ontology.md for the graph projection rules ``graph_store.py`` enforces.
"""

from __future__ import annotations

from .db import Database, get_database
from .graph_store import Neo4jGraphStore
from .repositories import (
    ArtifactRepo,
    AuditRepo,
    ChunkRepo,
    EmbeddingRepo,
    EntityRepo,
    EpisodeRepo,
    FactRepo,
    JobRepo,
    MetricsRepo,
    ProjectRepo,
    RunRepo,
    SourceRepo,
)

__all__ = [
    "ArtifactRepo",
    "AuditRepo",
    "ChunkRepo",
    "Database",
    "EmbeddingRepo",
    "EntityRepo",
    "EpisodeRepo",
    "FactRepo",
    "JobRepo",
    "MetricsRepo",
    "Neo4jGraphStore",
    "ProjectRepo",
    "RunRepo",
    "SourceRepo",
    "get_database",
]
