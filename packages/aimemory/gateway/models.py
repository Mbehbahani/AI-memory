"""Gateway response shapes that the frozen P1 contract does not already define.

``aimemory.domain.retrieval`` owns everything the *search* path returns (``SearchQuery``,
``ScoredHit``, ``RelatedEntity``, ``ContextBlock``, ``AssembledContext``, ``SearchResult``,
``ExplainChain``) and ``aimemory.domain.provenance`` owns the stamp. Those are frozen and only A02
may change them (agent protocol). The registry-shaped answers of plan section Q - projects,
entities, decisions, the timeline, sources, artifacts, current state, write receipts and health -
have no contract type, so they are defined here, in A09's own package, and nothing in ``domain/`` is
touched.

Every model is a strict :class:`~aimemory.domain.base.DomainModel` (``extra="forbid"``), so a typo in
a field name is a loud failure rather than a key silently dropped on the way to the REST layer.

Two properties are load-bearing across this file:

* **Track is always explicit.** ``track`` travels on every project-scoped view, because plan section
  Q requires business and research results to be labelled and never merged silently.
* **Coverage is honest.** :class:`ProjectCoverage` reports what is actually ingested, extracted and
  embedded, plus a plain-language ``note``; ADR-0006 tiering means "the memory does not know" is a
  legitimate and frequent answer, and the Gateway has to be able to say it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from ..domain.base import DomainModel
from ..domain.enums import ArtifactType, EntityType, ObjectType
from ..domain.provenance import Provenance
from ..domain.retrieval import RelatedEntity

__all__ = [
    "ArtifactView",
    "ComponentHealth",
    "CurrentState",
    "EntityView",
    "FactView",
    "HealthReport",
    "IngestionSummary",
    "MetricsSnapshotView",
    "ProjectCoverage",
    "ProjectView",
    "RelatedResult",
    "SourceView",
    "TimelineEvent",
    "WriteReceipt",
]


class ProjectCoverage(DomainModel):
    """What the memory actually holds for one project - ADR-0006's "honest coverage"."""

    project_id: str
    sources_total: int = 0
    sources_indexable: int = 0
    sources_embedded: int = 0
    chunks: int = 0
    episodes_total: int = 0
    episodes_extracted: int = 0
    episodes_failed: int = 0
    entities: int = 0
    facts: int = 0
    artifacts: int = 0
    embed_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    extraction_coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    note: str = Field(
        default="",
        description="Plain-language coverage statement, safe to show a caller verbatim",
    )


class ProjectView(DomainModel):
    """A ``projects`` row plus its aliases and coverage (``GET /v1/projects``)."""

    id: str
    name: str
    track: str
    status: str
    parent_id: str | None = None
    summary: str | None = None
    goal_ids: list[str] = Field(default_factory=list)
    root_ids: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    coverage: ProjectCoverage | None = None


class FactView(DomainModel):
    """One fact with its ``[PROV]`` stamp and rendered citation (``GET /v1/entities/{id}``)."""

    id: UUID
    statement: str
    predicate: str
    subject_entity_id: UUID | None = None
    subject_name: str | None = None
    object_entity_id: UUID | None = None
    object_name: str | None = None
    status: str = "current"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    project_id: str | None = None
    track: str = "unknown"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    observed_at: datetime | None = None
    provenance: Provenance | None = None
    citation: str = ""


class EntityView(DomainModel):
    """An entity, the facts current at ``as_of``, and where the knowledge came from."""

    id: UUID
    type: EntityType
    canonical_name: str
    normalized_name: str = ""
    aliases: list[str] = Field(default_factory=list)
    project_id: str | None = None
    track: str = "unknown"
    summary: str | None = None
    status: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    engine: str = "deterministic"
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    mention_count: int = 0
    facts: list[FactView] = Field(default_factory=list)
    provenance: list[Provenance] = Field(default_factory=list)
    related: list[RelatedEntity] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ArtifactView(DomainModel):
    """A ``knowledge_artifacts`` row with its supersession chain (decisions, tasks, findings, ...)."""

    id: UUID
    type: ArtifactType
    title: str
    body: str
    structured: dict[str, Any] = Field(default_factory=dict)
    status: str = "current"
    project_id: str | None = None
    track: str = "unknown"
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_quote: str | None = None
    supersedes_id: UUID | None = None
    supersedes_title: str | None = None
    superseded_by_id: UUID | None = None
    superseded_by_title: str | None = None
    entity_ids: list[UUID] = Field(default_factory=list)
    source_status: str = "active"
    provenance: Provenance | None = None
    citation: str = ""


class TimelineEvent(DomainModel):
    """One dated event on the merged timeline (``temporal.md`` §8).

    ``kind`` is the axis the date came from, so a caller can tell "the file changed" from "the fact
    became valid" instead of seeing one undifferentiated stream.
    """

    at: datetime
    kind: str = Field(description="source_version | episode | fact | artifact | run")
    object_type: ObjectType | None = None
    object_id: UUID | None = None
    title: str = ""
    project_id: str | None = None
    track: str = "unknown"
    detail: dict[str, str] = Field(default_factory=dict)


class SourceView(DomainModel):
    """A ``sources`` row - URIs and metadata only. The Gateway never returns file bytes."""

    id: UUID
    uri: str
    root_id: str
    relative_path: str
    project_id: str | None = None
    track: str = "unknown"
    kind: str = "file"
    media_type: str | None = None
    policy: str = "CATALOG_ONLY"
    policy_reason: str | None = None
    status: str = "active"
    origin: str = "internal"
    trust: str = "high"
    secret_suspected: bool = False
    container_path: str | None = Field(
        default=None,
        description="Mapped container path, only when the root is currently mounted (never bytes)",
    )
    current_version_id: UUID | None = None
    content_hash: str | None = None
    size_bytes: int | None = None
    observed_at: datetime | None = None
    last_seen_at: datetime | None = None
    versions: int = 0
    chunks: int = 0


class IngestionSummary(DomainModel):
    """The most recent ``ingestion_runs`` row - "when did the memory last learn anything?"."""

    run_id: UUID | None = None
    root_id: str | None = None
    tier: int | None = None
    trigger: str | None = None
    status: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counters: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class CurrentState(DomainModel):
    """``GET /v1/state`` - status, current facts, open tasks, latest decisions, ingestion, coverage.

    Results are grouped by track in :attr:`by_track` and every nested view carries its own ``track``;
    nothing is merged across tracks without a label (plan section Q).
    """

    as_of: datetime
    project_ids: list[str] = Field(default_factory=list)
    projects: list[ProjectView] = Field(default_factory=list)
    current_facts: list[FactView] = Field(default_factory=list)
    open_tasks: list[ArtifactView] = Field(default_factory=list)
    latest_decisions: list[ArtifactView] = Field(default_factory=list)
    last_ingestion: IngestionSummary | None = None
    coverage: list[ProjectCoverage] = Field(default_factory=list)
    by_track: dict[str, list[str]] = Field(
        default_factory=dict, description="track -> project ids included in this answer"
    )
    warnings: list[str] = Field(default_factory=list)


class RelatedResult(DomainModel):
    """``GET /v1/related`` - graph expansion on its own, with its degradation warning."""

    entity_ids: list[UUID] = Field(default_factory=list)
    related: list[RelatedEntity] = Field(default_factory=list)
    as_of: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    degraded: bool = False


class WriteReceipt(DomainModel):
    """ADR-0008 append-only write result. ``accepted=False`` carries the reason, never a stack."""

    accepted: bool
    object_type: ObjectType | None = None
    object_id: UUID | None = None
    episode_id: UUID | None = None
    message: str = ""


class ComponentHealth(DomainModel):
    """One dependency's liveness. ``detail`` is sanitized - never a DSN, path or credential."""

    name: str
    ok: bool
    detail: str = ""
    latency_ms: int = Field(default=0, ge=0)


class HealthReport(DomainModel):
    """``GET /health``: postgres, neo4j (as the read user) and the embedding service."""

    status: str = Field(description="ok | degraded | down")
    version: str = "0.1.0"
    checks: list[ComponentHealth] = Field(default_factory=list)
    writes_enabled: bool = False
    at: datetime | None = None

    @property
    def healthy(self) -> bool:
        return self.status != "down"


class MetricsSnapshotView(DomainModel):
    """``GET /metrics`` - simple in-process counters plus a few corpus totals (plan section Q).

    Counters are MEASURED since process start; corpus totals are MEASURED at request time. Nothing
    here is an estimate, and nothing here is a target.
    """

    at: datetime
    uptime_seconds: float = Field(ge=0.0)
    requests_total: dict[str, int] = Field(default_factory=dict)
    responses_total: dict[str, int] = Field(default_factory=dict)
    errors_total: dict[str, int] = Field(default_factory=dict)
    latency_ms: dict[str, float] = Field(default_factory=dict)
    corpus: dict[str, int] = Field(default_factory=dict)
    warnings_total: dict[str, int] = Field(default_factory=dict)
