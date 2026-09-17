"""``aimemory.gateway`` - the Memory Gateway service functions (plan section Q). Owner: A09.

:class:`~aimemory.gateway.service.Gateway` is the whole public surface: ``apps/memory-api`` maps REST
routes onto it one-for-one and adds nothing but serialization, and ``apps/mcp-server`` (A10) reaches
the same functions through that REST layer, holding no database credentials of its own (ADR-0008).

Layout:

===========================================  ==================================================
Module                                       Contents
===========================================  ==================================================
:mod:`aimemory.gateway.service`              ``Gateway`` - search, reads, state, health, writes
:mod:`aimemory.gateway.queries`              the SQL behind the registry-shaped routes
:mod:`aimemory.gateway.writes`               ``add_episode`` / ``record_decision`` (ADR-0008)
:mod:`aimemory.gateway.models`               response shapes the frozen P1 contract lacks
===========================================  ==================================================

Nothing here re-implements a retrieval stage: :mod:`aimemory.retrieval` owns all eight, and the
Gateway composes them in one read transaction per call.
"""

from .models import (
    ArtifactView,
    ComponentHealth,
    CurrentState,
    EntityView,
    FactView,
    HealthReport,
    IngestionSummary,
    MetricsSnapshotView,
    ProjectCoverage,
    ProjectView,
    RelatedResult,
    SourceView,
    TimelineEvent,
    WriteReceipt,
)
from .service import Gateway, SessionScope
from .writes import add_episode, record_decision

__all__ = [
    "ArtifactView",
    "ComponentHealth",
    "CurrentState",
    "EntityView",
    "FactView",
    "Gateway",
    "HealthReport",
    "IngestionSummary",
    "MetricsSnapshotView",
    "ProjectCoverage",
    "ProjectView",
    "RelatedResult",
    "SessionScope",
    "SourceView",
    "TimelineEvent",
    "WriteReceipt",
    "add_episode",
    "record_decision",
]
