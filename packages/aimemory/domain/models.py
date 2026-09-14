"""Domain models - one pydantic class per PostgreSQL table of plan section G (plus ADR-0010/0011).

Consumers:

* **A04** builds Alembic migration 0001/0002 and the SQLAlchemy repositories from these classes;
  ``docs/architecture/data-model.md`` gives the exact column types and indexes.
* **A07a/A07b** construct them during ingestion; **A08** during extraction and temporal updates.
* **A09** returns them (or the DTOs in :mod:`aimemory.domain.retrieval`) from the Gateway;
  **A10** serializes them for MCP; **A11/A16** read them for dashboards; **A12** tests them.

Conventions

* Surrogate keys are ``UUID`` (:func:`aimemory.common.ids.new_id`, UUIDv7 layout). Registry keys a
  human types - ``projects.id``, ``source_roots.root_id``, ``devices.id``, model ids - are slugs
  (``str``), because they appear in ``config/*.yaml`` and in provenance output.
* Every timestamp is timezone-aware UTC (:mod:`aimemory.common.time`).
* Derived rows (``entity_mentions``, ``facts``, ``knowledge_artifacts``) embed a
  :class:`~aimemory.domain.provenance.Provenance`; in Postgres it is flattened into the ``[PROV]``
  columns of plan section J, not stored as JSON.
* A model is a *contract*, not an ORM row: no lazy relationships, no database session, no defaults
  that hide a missing value.

Frozen after P1 (agent protocol): changes require an ADR.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from ..common.time import utc_now
from .base import DomainModel, OpenModel
from .enums import (
    ArtifactStatus,
    ArtifactType,
    ChangeType,
    EngineKind,
    EntityType,
    EpisodeStatus,
    EpisodeType,
    FactStatus,
    JobStage,
    JobState,
    ObjectType,
    Origin,
    Predicate,
    ProjectStatus,
    ReviewVerdict,
    RunAction,
    RunRequestStatus,
    RunStatus,
    RunTrigger,
    SourceEventType,
    SourceKind,
    SourceStatus,
    SourceUriScheme,
    StoragePolicy,
    Tier,
    Track,
    Trust,
)
from .provenance import Provenance

__all__ = [
    "ArtifactEntity",
    "Chunk",
    "Device",
    "Embedding",
    "EmbeddingModel",
    "Entity",
    "EntityMention",
    "Episode",
    "ExtractionModel",
    "ExtractionReview",
    "Fact",
    "IngestionJob",
    "IngestionRun",
    "KnowledgeArtifact",
    "McpAuditLog",
    "MetricsSnapshot",
    "MirrorBlob",
    "Project",
    "ProjectAlias",
    "RetrievalLog",
    "RunRequest",
    "ServiceStat",
    "Source",
    "SourceEvent",
    "SourceFrontmatter",
    "SourceRoot",
    "SourceText",
    "SourceVersion",
]


# --------------------------------------------------------------------------------------------------
# Registries
# --------------------------------------------------------------------------------------------------


class Device(DomainModel):
    """``devices`` - a logical machine that holds sources (ADR-0004).

    V0.1 has exactly one row, ``local-development-machine``, seeded by migration 0001. It exists so
    that provenance is portable the day a second device appears; multi-device connectors are out of
    scope. Consumers: A04 (seed), A08 (``STORED_ON`` edges), A09 (explain output).
    """

    id: str = Field(description="Slug, e.g. 'local-development-machine'")
    label: str
    os: str | None = None
    notes: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Project(DomainModel):
    """``projects`` - the registry seeded in Tier 0 from ``AIOS/me.md`` and
    ``AIOS/Maps/project-graph.md`` (plan section G, task P7-T01).

    ``id`` is a slug (``joblab-lakehouse``) used everywhere as the scoping key: retrieval filters,
    MCP ``project_id`` arguments, Neo4j ``project_id`` property. ``parent_id`` expresses the
    sub-project relation, which is projected as ``:Project`` + ``PART_OF`` rather than a separate
    label. Consumers: A04, A07a (registry seed), A08 (scoping), A09, A10, A16 (coverage).
    """

    id: str = Field(description="Slug primary key")
    name: str
    track: Track
    parent_id: str | None = None
    status: ProjectStatus = ProjectStatus.UNKNOWN
    goal_ids: list[str] = Field(default_factory=list, description="Goal ids from AIOS/me.md (G1, ...)")
    summary: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    root_ids: list[str] = Field(default_factory=list, description="source_roots this project lives in")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _no_self_parent(self) -> Project:
        if self.parent_id is not None and self.parent_id == self.id:
            raise ValueError("project cannot be its own parent")
        return self


class ProjectAlias(DomainModel):
    """``project_aliases`` - every spelling that must resolve to one project.

    Seeded from ``config/technology-aliases.yaml: projects`` plus aliases discovered during
    ingestion. ``normalized_alias`` is :func:`aimemory.common.ids.normalize_name` output and carries
    a unique index, which is what stops "JobLab DE" and "JobLab Lakehouse (DE)" becoming two nodes.
    Consumers: A04, A07a, A08 (deterministic-first entity resolution).
    """

    id: UUID
    project_id: str
    alias: str
    normalized_alias: str
    source: str = Field(default="config", description="config | registry | extraction | manual")


class SourceRoot(DomainModel):
    """``source_roots`` - one entry of ``config/source-roots.yaml`` (AC-5).

    Host paths never appear here: only the container path and the URI scheme/label (ADR-0004).
    ``priority_paths`` drives the Tier 2 queue order (ADR-0006); ``mirror_paths`` is the opt-in
    MIRROR list (ADR-0003); ``origin_overrides`` carries AC-6 (vault ``Clippings/`` is
    ``origin=external, trust=low``). Consumers: A04 (seed), A07a (discovery), A09 (URI resolution).
    """

    root_id: str
    scheme: SourceUriScheme
    label: str = Field(description="Label used in the URI authority position")
    container_path: PurePosixPath = Field(description="Mount point inside the container, e.g. /sources/vault")
    device_id: str = "local-development-machine"
    enabled: bool = True
    default_policy: StoragePolicy = StoragePolicy.INDEX_CONTENT
    default_project_id: str | None = None
    kind: SourceKind = SourceKind.DIRECTORY
    registry_role: str | None = Field(default=None, description="'bootstrap' seeds the registry")
    priority_paths: list[str] = Field(default_factory=list)
    mirror_paths: list[str] = Field(default_factory=list)
    exclude_extra: list[str] = Field(default_factory=list)
    origin_overrides: list[dict[str, str]] = Field(default_factory=list)

    @field_validator("container_path", mode="before")
    @classmethod
    def _posix(cls, value: object) -> object:
        """Accept Windows separators in config, store a POSIX container path."""
        return str(value).replace("\\", "/") if value is not None else value


class EmbeddingModel(DomainModel):
    """``embedding_models`` - identity of a vector producer (plan section N).

    A model change is *additive*: a new row, new vectors, old vectors kept, because
    ``embeddings.model_id`` is part of the reuse key. Consumers: A04, A06, A09.
    """

    id: str = Field(description="Slug, e.g. 'minilm-l6-v2-384'")
    name: str = Field(description="sentence-transformers/all-MiniLM-L6-v2")
    dimension: int = Field(ge=1)
    revision: str | None = None
    normalized: bool = True
    max_seq: int = Field(default=256, ge=1)
    created_at: datetime = Field(default_factory=utc_now)


class ExtractionModel(DomainModel):
    """``extraction_models`` - identity of a knowledge producer (plan section M, ADR-0010).

    Holds both LLMs (``qwen3:4b`` with its Ollama digest) and the deterministic pseudo-model
    ``deterministic:registry-v1``. Every fact and artifact stamps ``extraction_model_id``, so after a
    model upgrade provenance still says which model produced which row. Consumers: A04, A05, A08, A16.
    """

    id: str = Field(description="Slug or 'deterministic:registry-v1'")
    provider: str = Field(default="ollama", description="ollama | deterministic")
    name: str = Field(description="qwen3:4b")
    digest: str | None = Field(default=None, description="Ollama model digest, when known")
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


# --------------------------------------------------------------------------------------------------
# Sources and content
# --------------------------------------------------------------------------------------------------


class Source(DomainModel):
    """``sources`` - one registered file/directory/repository, identified by its logical URI.

    ``uri`` is unique. A rename keeps the same ``id`` and rewrites ``uri`` (plan section L: moved),
    recording ``moved_from_uri``; a deletion sets ``status=deleted`` and never removes knowledge
    (ADR-0005 rule 4). ``secret_suspected`` means the secret detector matched: metadata only, no text
    stored, nothing logged (plan section K). Consumers: A04, A07a, A08, A09.
    """

    id: UUID
    uri: str
    root_id: str
    relative_path: str = Field(description="POSIX relative path inside the root; never absolute")
    project_id: str | None = None
    kind: SourceKind = SourceKind.FILE
    media_type: str | None = None
    policy: StoragePolicy = StoragePolicy.CATALOG_ONLY
    policy_reason: str | None = Field(default=None, description="Which rule decided the policy")
    status: SourceStatus = SourceStatus.ACTIVE
    moved_from_uri: str | None = None
    current_version_id: UUID | None = None
    secret_suspected: bool = False
    origin: Origin = Origin.INTERNAL
    trust: Trust = Trust.HIGH
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)


class SourceVersion(DomainModel):
    """``source_versions`` - an immutable content state of a source.

    Unique on ``(source_id, content_hash)``: re-observing identical bytes reuses the version instead
    of creating one. ``is_current`` marks the newest. ``git_commit``/``git_branch`` are version
    metadata only - git history is not ingested as episodes in V0.1 (assumption B13).
    Consumers: A04, A07a (change detection), A08 (provenance), A09 (explain).
    """

    id: UUID
    source_id: UUID
    content_hash: str = Field(description="sha256:<hex> of the raw bytes")
    size_bytes: int = Field(ge=0)
    mtime: datetime | None = None
    git_commit: str | None = None
    git_branch: str | None = None
    observed_at: datetime
    ingestion_run_id: UUID | None = None
    is_current: bool = True
    change_type: ChangeType = ChangeType.NEW
    created_at: datetime = Field(default_factory=utc_now)


class SourceText(DomainModel):
    """``source_text`` - extracted plain text for one version (INDEX_CONTENT / MIRROR only).

    One row per version; ``truncated`` records that ``INGEST_MAX_TEXT_BYTES`` cut the content.
    ``frontmatter`` and ``links`` are the deterministic structure used by the Tier 0/1 structural
    graph (Obsidian YAML frontmatter and ``[[wikilinks]]``/markdown links).
    Consumers: A04, A07b (extractors), A07a, A08 (episodes), A09 (never returns raw file bytes).
    """

    version_id: UUID
    text: str
    extractor: str = Field(description="markdown | plaintext | pdf | docx | ipynb | code")
    extractor_version: str = "0.1.0"
    char_count: int = Field(ge=0)
    truncated: bool = False
    frontmatter: dict[str, object] = Field(default_factory=dict)
    links: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class MirrorBlob(DomainModel):
    """``mirror_blobs`` - raw bytes for MIRROR sources (ADR-0003: Postgres ``bytea``, no MinIO).

    Opt-in per root (``mirror_paths``). Revisit with a new ADR if the mirrored volume exceeds
    ~500 MB. Consumers: A04, A07a. The Gateway never returns these bytes (plan section Q).
    """

    version_id: UUID
    media_type: str
    size_bytes: int = Field(ge=0)
    data: bytes
    created_at: datetime = Field(default_factory=utc_now)


class Chunk(DomainModel):
    """``chunks`` - a retrievable slice of one version's text.

    ``text_hash`` is the embedding-reuse key together with ``embeddings.model_id``: a document edit
    that leaves a chunk untouched costs no embedding work (plan section L, modified case).
    ``heading_path`` + offsets are the chunk part of the provenance stamp and produce the citation
    ``[{source_uri}#{heading} @{hash8}]``. The Postgres ``tsvector`` column is generated in the DDL,
    not carried here. Consumers: A07b (chunkers), A04, A06, A09 (candidates).
    """

    id: UUID
    version_id: UUID
    source_id: UUID
    project_id: str | None = None
    ordinal: int = Field(ge=0)
    text: str
    text_hash: str
    heading_path: list[str] = Field(default_factory=list)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    token_count: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _offsets_ordered(self) -> Chunk:
        if self.char_end < self.char_start:
            raise ValueError("char_end must be >= char_start")
        return self


class Embedding(DomainModel):
    """``embeddings`` - one vector for one ``(object, model)``.

    Polymorphic by ``(object_type, object_id)`` so chunks, artifacts and entity summaries share the
    table and the HNSW cosine index. Reuse key: ``(text_hash, model_id)``. ``vector`` length must
    equal the model dimension (384 for MiniLM-L6-v2); the check lives here so a mis-sized vector
    never reaches pgvector. Consumers: A04 (``vector(384)`` column + HNSW), A06, A09.
    """

    id: UUID
    object_type: ObjectType
    object_id: UUID
    text_hash: str
    model_id: str
    dimension: int = Field(ge=1)
    vector: list[float]
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _dimension_matches(self) -> Embedding:
        if len(self.vector) != self.dimension:
            raise ValueError(f"vector has {len(self.vector)} values, dimension says {self.dimension}")
        return self


# --------------------------------------------------------------------------------------------------
# Episodes and knowledge
# --------------------------------------------------------------------------------------------------


class Episode(DomainModel):
    """``episodes`` - a unit of memory change; the input to :class:`~aimemory.domain.ports.KnowledgeEngine`.

    One episode per document (or document section) version, per registry seed, per manual note, per
    MCP write. ``status``/``attempts``/``error`` drive the Tier 2 queue and the ADR-0010 validity
    metric. ``graph_episode_uuid`` holds Graphiti's own episode id when that engine is selected
    (ADR-0002), so its Neo4j structures can be correlated without the Gateway ever reading them.
    ``priority`` is the ADR-0006 ordering value (lower = earlier). Consumers: A04, A07a, A08, A16.
    """

    id: UUID
    type: EpisodeType
    source_id: UUID | None = None
    version_id: UUID | None = None
    project_id: str | None = None
    title: str | None = None
    section_path: list[str] = Field(default_factory=list)
    body: str | None = Field(default=None, description="Episode text; may be a section of source_text")
    occurred_at: datetime | None = None
    observed_at: datetime
    status: EpisodeStatus = EpisodeStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    engine: EngineKind | None = None
    graph_episode_uuid: str | None = None
    origin: Origin = Origin.INTERNAL
    origin_client: str | None = Field(default=None, description="MCP client id for origin=mcp writes")
    tier: Tier = Tier.KNOWLEDGE
    priority: int = Field(default=100, description="ADR-0006 queue order; lower runs first")
    error: str | None = Field(default=None, description="Sanitized error text; never raw content")
    ingestion_run_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Entity(DomainModel):
    """``entities`` - a resolved node of the knowledge graph.

    ``normalized_name`` (trigram-indexed) plus ``type`` is the deterministic resolution key;
    ``aliases`` accumulates observed spellings. A merge sets ``merged_into_id`` on the loser and
    keeps the row, so old provenance still resolves. ``type`` is an ontology label; ``SubProject`` is
    stored as ``Project`` + ``parent`` in Neo4j (see :class:`~aimemory.domain.enums.EntityType`).
    Consumers: A04, A08 (resolution + projection), A09 (graph expansion), A10, A11.
    """

    id: UUID
    type: EntityType
    canonical_name: str
    normalized_name: str
    aliases: list[str] = Field(default_factory=list)
    project_id: str | None = None
    summary: str | None = None
    status: str | None = Field(default=None, description="Free-form status for Task/Decision-like nodes")
    merged_into_id: UUID | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    engine: EngineKind = EngineKind.DETERMINISTIC
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)


class EntityMention(DomainModel):
    """``entity_mentions`` ``[PROV]`` - where an entity was seen, with evidence.

    The join that answers "which sentence made you think this entity exists". Consumers: A04, A08,
    A09 (entity-linked boost and evidence quotes), A10.
    """

    id: UUID
    entity_id: UUID
    episode_id: UUID
    chunk_id: UUID | None = None
    surface_form: str
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    provenance: Provenance


class Fact(DomainModel):
    """``facts`` ``[PROV]`` - a temporal triple ``(subject, predicate, object)``.

    The object is either another entity (``object_entity_id``) or a literal (``object_value``) - the
    functional predicates of the ontology (``HAS_STATUS``, ``SELECTED_OPTION``, ...) are usually
    literal-valued. Exactly one of the two must be set.

    Temporal semantics (ADR-0005): ``valid_from``/``valid_to`` define the validity window,
    ``status`` is ``current`` | ``historical`` | ``unconfirmed``, ``supersedes_fact_id`` links the
    closed fact, ``invalidated_at``/``invalidated_by_episode_id`` record who closed it, and
    ``source_status`` propagates a deleted source without erasing anything.
    Consumers: A04, A08 (the supersession algorithm in ``docs/architecture/temporal.md``),
    A09 (``as_of`` filter + unconfirmed penalty), A10, A11.
    """

    id: UUID
    subject_entity_id: UUID
    predicate: Predicate
    object_entity_id: UUID | None = None
    object_value: str | None = None
    statement: str = Field(description="One-sentence natural-language form, used for embedding/display")
    valid_from: datetime
    valid_to: datetime | None = None
    observed_at: datetime
    status: FactStatus = FactStatus.CURRENT
    source_status: SourceStatus = SourceStatus.ACTIVE
    invalidated_at: datetime | None = None
    invalidated_by_episode_id: UUID | None = None
    supersedes_fact_id: UUID | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    engine: EngineKind = EngineKind.DETERMINISTIC
    project_id: str | None = None
    provenance: Provenance

    @model_validator(mode="after")
    def _object_exactly_once(self) -> Fact:
        has_entity = self.object_entity_id is not None
        has_value = self.object_value is not None
        if has_entity == has_value:
            raise ValueError("set exactly one of object_entity_id / object_value")
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be >= valid_from")
        return self

    @property
    def object_key(self) -> str:
        """The value compared when deciding whether a functional predicate changed."""
        return str(self.object_entity_id) if self.object_entity_id else (self.object_value or "")


class KnowledgeArtifact(DomainModel):
    """``knowledge_artifacts`` ``[PROV]`` - a decision, requirement, task, finding, hypothesis,
    experiment or summary.

    Artifacts are the human-facing knowledge unit: ``memory.get_decisions``, the Decisions Timeline
    dashboard and ``record_decision`` all operate on this table. Supersession is a *state*, not a
    deletion (ADR-0005 rule 6): ``supersedes_id``/``superseded_by_id`` form the chain and
    ``current_status`` becomes ``superseded``. ``structured`` holds type-specific extras
    (options considered, outcome, acceptance criteria). Consumers: A04, A08, A09, A10, A11, A16.
    """

    id: UUID
    type: ArtifactType
    title: str
    body: str
    structured: dict[str, object] = Field(default_factory=dict)
    project_id: str | None = None
    current_status: ArtifactStatus = ArtifactStatus.CURRENT
    valid_from: datetime
    valid_to: datetime | None = None
    supersedes_id: UUID | None = None
    superseded_by_id: UUID | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_quote: str | None = Field(default=None, max_length=300)
    engine: EngineKind = EngineKind.DETERMINISTIC
    source_status: SourceStatus = SourceStatus.ACTIVE
    provenance: Provenance

    @model_validator(mode="after")
    def _window_ordered(self) -> KnowledgeArtifact:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be >= valid_from")
        return self


class ArtifactEntity(DomainModel):
    """``artifact_entities`` - which entities an artifact is about, with a role.

    Drives graph expansion from a decision to its technologies/projects and the reverse lookup
    "which decisions touch pgvector". Consumers: A04, A08, A09.
    """

    artifact_id: UUID
    entity_id: UUID
    role: str = Field(default="mentions", description="about | mentions | produces | affects")


# --------------------------------------------------------------------------------------------------
# Operations and audit
# --------------------------------------------------------------------------------------------------


class IngestionRun(DomainModel):
    """``ingestion_runs`` - one scan (``aimemory-ingest run`` or a worker-executed request).

    ``counters`` holds the honest per-run numbers the Ops page and ``status`` display
    (discovered/new/modified/unchanged/embedded/episodes/failed). Consumers: A04, A07a, A16.
    """

    id: UUID
    root_id: str | None = Field(default=None, description="NULL = all enabled roots")
    tier: Tier = Tier.KNOWLEDGE
    trigger: RunTrigger = RunTrigger.CLI
    requested_by: str | None = None
    status: RunStatus = RunStatus.PENDING
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    counters: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class IngestionJob(DomainModel):
    """``ingestion_jobs`` - the resumable per-``(source_version, stage)`` state machine of plan L.

    Unique on ``(version_id, stage)``. Restart policy: rows left ``running`` by a killed process are
    re-queued, and every stage's write is idempotent on that pair. Consumers: A04, A07a, A12, A16.
    """

    id: UUID
    run_id: UUID
    version_id: UUID | None = None
    source_id: UUID | None = None
    stage: JobStage
    state: JobState = JobState.PENDING
    attempts: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, description="Sanitized; never file content")


class SourceEvent(DomainModel):
    """``source_events`` - append-only history of what happened to a source (plan section G).

    Never updated, never deleted: this is the audit trail behind "the file moved, the knowledge
    stayed". Consumers: A04, A07a, A09 (``/v1/timeline``), A16 (attention list).
    """

    id: UUID
    source_id: UUID
    version_id: UUID | None = None
    event_type: SourceEventType
    at: datetime = Field(default_factory=utc_now)
    run_id: UUID | None = None
    details: dict[str, str] = Field(default_factory=dict)


class RetrievalLog(DomainModel):
    """``retrieval_logs`` - one row per Gateway query (plan section P).

    Stores the query, the scoping, the tuning parameters actually used and the returned object ids,
    so an evaluation run can be replayed and a ranking regression can be attributed. The query text
    is stored; no file content is. Consumers: A04, A09, A12 (gold-set evaluation), A16.
    """

    id: UUID
    at: datetime = Field(default_factory=utc_now)
    query_text: str
    query_hash: str
    project_ids: list[str] = Field(default_factory=list)
    object_types: list[ObjectType] = Field(default_factory=list)
    as_of: datetime | None = None
    since: datetime | None = None
    limit: int = Field(default=10, ge=1)
    expand: bool = True
    params: dict[str, float] = Field(default_factory=dict, description="Effective weights/boosts")
    candidate_counts: dict[str, int] = Field(default_factory=dict, description="semantic/keyword/graph")
    result_ids: list[UUID] = Field(default_factory=list)
    latency_ms: int = Field(default=0, ge=0)
    client: str | None = None
    warnings: list[str] = Field(default_factory=list)


class McpAuditLog(DomainModel):
    """``mcp_audit_log`` - ADR-0008. Every MCP call, especially every refused one.

    ``arguments`` is stored redacted (no free text beyond the size limit, no secrets). ``allowed``
    is false when the write flags were off, ``confirm`` was missing, or the rate limit fired.
    Append-only. Consumers: A04, A10, A13 (security review), A16 (attention list).
    """

    id: UUID
    at: datetime = Field(default_factory=utc_now)
    tool: str
    kind: str = Field(description="read | write")
    client_id: str | None = None
    arguments: dict[str, object] = Field(default_factory=dict)
    confirmed: bool = False
    allowed: bool = True
    denied_reason: str | None = None
    result_ref: str | None = Field(default=None, description="Created episode/artifact id, if any")
    latency_ms: int | None = Field(default=None, ge=0)


# --------------------------------------------------------------------------------------------------
# ADR-0010 / ADR-0011 additions
# --------------------------------------------------------------------------------------------------


class MetricsSnapshot(DomainModel):
    """``metrics_snapshots`` (ADR-0010) - quality/throughput numbers after a run or an eval.

    Written by the ingestion worker and by ``scripts/eval``; read by ``aimemory-ingest status``, the
    NeoDash Health page and the Ops page. Every value is MEASURED at write time; ``None`` means the
    metric was not computed in that scope, never zero-by-default. Consumers: A04, A07a, A11, A12, A16.
    """

    id: UUID
    at: datetime = Field(default_factory=utc_now)
    scope: str = Field(description="ingestion_run | eval | benchmark")
    run_id: UUID | None = None
    model_id: str | None = None
    schema_validity_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    failed_episode_share: float | None = Field(default=None, ge=0.0, le=1.0)
    median_seconds_per_episode: float | None = Field(default=None, ge=0.0)
    duplicate_entity_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    unconfirmed_fact_share: float | None = Field(default=None, ge=0.0, le=1.0)
    coverage_by_project: dict[str, float] = Field(default_factory=dict)
    gold_hit_at_5: float | None = Field(default=None, ge=0.0, le=1.0)
    expected_entity_presence: float | None = Field(default=None, ge=0.0, le=1.0)
    provenance_completeness: float | None = Field(default=None, ge=0.0, le=1.0)
    extra: dict[str, float] = Field(default_factory=dict)


class ExtractionReview(DomainModel):
    """``extraction_reviews`` (ADR-0010) - one human verdict on one extracted artifact or fact.

    Acceptance rate over these rows is the primary extraction-quality number and the trigger for the
    model-upgrade rule (< 70 % over two consecutive reviews). Consumers: A04, A07a (``review`` CLI),
    A16 (review form), A12.
    """

    id: UUID
    at: datetime = Field(default_factory=utc_now)
    object_type: ObjectType
    object_id: UUID
    verdict: ReviewVerdict
    reviewer: str = Field(default="owner")
    note: str | None = None
    episode_id: UUID | None = None
    model_id: str | None = None
    sample_batch: str | None = Field(default=None, description="Groups the 20 items of one review")


class RunRequest(DomainModel):
    """``run_requests`` (ADR-0011) - the queue the always-on ingestion worker polls.

    Created by the Ops page buttons and by the CLI; executed serially. Containers never get the
    Docker socket, so this table *is* the control plane, and the allowed actions are exactly
    :class:`~aimemory.domain.enums.RunAction`. Consumers: A04, A07a (worker), A16 (page), A09 (router).
    """

    id: UUID
    action: RunAction = RunAction.SCAN
    root_id: str | None = None
    tier: Tier = Tier.KNOWLEDGE
    options: dict[str, str] = Field(default_factory=dict)
    requested_by: str = Field(default="ops-page")
    requested_at: datetime = Field(default_factory=utc_now)
    status: RunRequestStatus = RunRequestStatus.QUEUED
    progress_pct: int = Field(default=0, ge=0, le=100)
    message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    run_id: UUID | None = Field(default=None, description="ingestion_runs.id once execution starts")
    error: str | None = None


class ServiceStat(DomainModel):
    """``service_stats`` (ADR-0011) - what a container can honestly measure about itself.

    ``docker stats`` is not reachable from inside a container, so this records the worker's own
    process RSS and Ollama's ``/api/ps`` model-loaded state. Host-level RAM is reported by
    ``scripts/doctor`` instead and is labelled as such on the Ops page. Consumers: A04, A07a, A16.
    """

    id: UUID
    at: datetime = Field(default_factory=utc_now)
    service: str = Field(description="ingestion | memory-api | ollama | embedding-service")
    process_rss_bytes: int | None = Field(default=None, ge=0, description="MEASURED, self-reported")
    model_loaded: bool | None = Field(default=None, description="Ollama /api/ps")
    model_name: str | None = None
    details: dict[str, str] = Field(default_factory=dict)


class SourceFrontmatter(OpenModel):
    """Obsidian/YAML frontmatter as parsed by A07b. Lenient on purpose: notes carry arbitrary keys.

    Not a table - it is the shape stored in ``source_text.frontmatter`` (JSONB). Consumers: A07b, A07a.
    """

    title: str | None = None
    project: str | None = None
    tags: list[str] = Field(default_factory=list)
    status: str | None = None
    created: datetime | None = None
    updated: datetime | None = None

