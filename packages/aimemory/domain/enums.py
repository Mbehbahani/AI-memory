"""Closed vocabularies of the AI Memory domain.

Consumers: A04 (Postgres ``CHECK`` constraints / enum columns and Alembic seeds), A07a (state
machine), A07b (policy resolution), A08 (temporal rules, engine identity, Neo4j projection),
A09 (retrieval filters and boosts), A10 (MCP argument validation), A11/A16 (dashboards),
A12 (tests assert full coverage against ``schemas/ontology.yaml`` and ``schemas/extraction/*.json``).

Everything here is a :class:`~enum.StrEnum`, so the member *is* the string stored in Postgres, sent
over REST/MCP, and written to Neo4j. Adding a member after P1 requires an ADR.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

__all__ = [
    "ArtifactStatus",
    "ArtifactType",
    "ChangeType",
    "DocKind",
    "EngineKind",
    "EntityType",
    "EpisodeStatus",
    "EpisodeType",
    "FactStatus",
    "JobStage",
    "JobState",
    "McpToolKind",
    "ObjectType",
    "Origin",
    "Predicate",
    "ProjectStatus",
    "RelationshipGroup",
    "RetrieverKind",
    "ReviewVerdict",
    "RunAction",
    "RunRequestStatus",
    "RunStatus",
    "RunTrigger",
    "SourceEventType",
    "SourceKind",
    "SourceStatus",
    "SourceUriScheme",
    "StatedArtifactStatus",
    "StoragePolicy",
    "Tier",
    "Track",
    "Trust",
]


class StoragePolicy(StrEnum):
    """Plan section K. Resolution order: deny list > ``.memoryignore`` > ``config/policies.yaml`` >
    per-root override > secret-detector downgrade. The detector can only *downgrade*."""

    IGNORE = "IGNORE"
    CATALOG_ONLY = "CATALOG_ONLY"
    INDEX_CONTENT = "INDEX_CONTENT"
    MIRROR = "MIRROR"

    @property
    def stores_text(self) -> bool:
        """True when ``source_text`` rows may be written for this source."""
        return self in (StoragePolicy.INDEX_CONTENT, StoragePolicy.MIRROR)

    @property
    def stores_bytes(self) -> bool:
        """True only for MIRROR, which writes ``mirror_blobs`` (ADR-0003)."""
        return self is StoragePolicy.MIRROR


class SourceStatus(StrEnum):
    """Plan section G ``sources.status``. Deletion never erases knowledge (ADR-0005 rule 4)."""

    ACTIVE = "active"
    DELETED = "deleted"
    MOVED = "moved"


class SourceKind(StrEnum):
    """What the source *is*, independent of its storage policy."""

    FILE = "file"
    DIRECTORY = "directory"
    REPOSITORY = "repository"
    NOTE = "note"


class SourceUriScheme(StrEnum):
    """ADR-0004. See :mod:`aimemory.domain.source_uri` for the grammar of each scheme."""

    VAULT = "vault"
    LOCALFS = "localfs"
    GIT = "git"


class Origin(StrEnum):
    """Where the content came from. AC-6: vault ``Clippings/`` is ``external``."""

    INTERNAL = "internal"
    EXTERNAL = "external"


class Trust(StrEnum):
    """Trust band attached to a source/episode; feeds the retrieval ranking (AC-6)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ChangeType(StrEnum):
    """Plan section L change-detection outcomes; one per ``(source, scan)``."""

    NEW = "new"
    MODIFIED = "modified"
    MOVED = "moved"
    DELETED = "deleted"
    UNCHANGED = "unchanged"
    DUPLICATE = "duplicate"


class Tier(IntEnum):
    """ADR-0006. Tier 0 registry (no LLM), Tier 1 chunk+embed (no LLM), Tier 2 LLM extraction."""

    REGISTRY = 0
    EMBED = 1
    KNOWLEDGE = 2


class JobStage(StrEnum):
    """Plan section L. One ``ingestion_jobs`` row per ``(source_version, stage)``; writes are
    idempotent on that pair so a killed process can be resumed."""

    DISCOVER = "discover"
    CLASSIFY = "classify"
    FINGERPRINT = "fingerprint"
    DIFF = "diff"
    EXTRACT_METADATA = "extract_metadata"
    EXTRACT_TEXT = "extract_text"
    CHUNK = "chunk"
    EMBED = "embed"
    EPISODE = "episode"
    EXTRACT_KNOWLEDGE = "extract_knowledge"
    VALIDATE = "validate"
    RESOLVE_ENTITIES = "resolve_entities"
    TEMPORAL = "temporal"
    PROJECT_GRAPH = "project_graph"
    PROVENANCE = "provenance"


class JobState(StrEnum):
    """State machine for one job. ``RUNNING`` rows from a dead process are re-queued on restart."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class EpisodeType(StrEnum):
    """Mirrors ``schemas/ontology.yaml: episode_types``."""

    DOCUMENT = "document"
    DOCUMENT_CHANGE = "document_change"
    REGISTRY = "registry"
    MANUAL = "manual"
    MCP = "mcp"


class EpisodeStatus(StrEnum):
    """Tier 2 queue state. ``FAILED`` keeps the error and stays reprocessable
    (``reprocess --failed``); vectors from Tier 1 are never discarded."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    EXTRACTED = "extracted"
    FAILED = "failed"
    SKIPPED = "skipped"


class EngineKind(StrEnum):
    """Which producer wrote a node/edge/row. Stamped on every graph element (plan section H)."""

    DETERMINISTIC = "deterministic"
    NATIVE = "native"
    GRAPHITI = "graphiti"


class FactStatus(StrEnum):
    """ADR-0005. ``UNCONFIRMED`` still has ``valid_to IS NULL`` - it is current but not re-observed
    in the newest source version, and is ranked down by ``boosts.unconfirmed_penalty``."""

    CURRENT = "current"
    HISTORICAL = "historical"
    UNCONFIRMED = "unconfirmed"


class ArtifactType(StrEnum):
    """Mirrors ``schemas/ontology.yaml: artifact_types``. ``SUMMARY`` is produced by the engine, not
    by the LLM artifact list, so it is absent from ``episode_extraction.schema.json``."""

    DECISION = "decision"
    REQUIREMENT = "requirement"
    TASK = "task"
    FINDING = "finding"
    HYPOTHESIS = "hypothesis"
    EXPERIMENT = "experiment"
    SUMMARY = "summary"


class ArtifactStatus(StrEnum):
    """Mirrors ``schemas/ontology.yaml: artifact_status`` - the stored status of an artifact.

    The extraction model emits :class:`StatedArtifactStatus` instead; ``historical`` and
    ``unconfirmed`` are only ever set by the temporal engine (ADR-0005 rules 1 and 3)."""

    CURRENT = "current"
    SUPERSEDED = "superseded"
    HISTORICAL = "historical"
    UNCONFIRMED = "unconfirmed"
    PROPOSED = "proposed"
    DONE = "done"
    ABANDONED = "abandoned"


class StatedArtifactStatus(StrEnum):
    """Status exactly as the extraction model is allowed to state it.

    Mirrors the ``artifacts[].status`` enum of
    ``schemas/extraction/episode_extraction.schema.json``. It is deliberately *not* the same set as
    :class:`ArtifactStatus`: ``historical`` and ``unconfirmed`` are decided by the temporal engine
    from evidence, never claimed by the model, and ``unknown`` is the model's way of abstaining.
    :meth:`to_artifact_status` is the only mapping A08 may use.
    """

    CURRENT = "current"
    PROPOSED = "proposed"
    DONE = "done"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"

    def to_artifact_status(self) -> ArtifactStatus:
        """``unknown`` becomes ``current`` (the engine lowers confidence); the rest map by name."""
        if self is StatedArtifactStatus.UNKNOWN:
            return ArtifactStatus.CURRENT
        return ArtifactStatus(self.value)


class EntityType(StrEnum):
    """The ontology node labels (``schemas/ontology.yaml: node_types``).

    ``SUB_PROJECT`` is a modelling type, not a distinct Neo4j label: it is stored as ``:Project`` with
    ``parent_id`` set (``stored_as: Project`` in the ontology file). That is why
    ``infra/neo4j/schema/constraints.cypher`` declares 18 uniqueness constraints for 19 types."""

    PROJECT = "Project"
    SUB_PROJECT = "SubProject"
    PERSON = "Person"
    ORGANIZATION = "Organization"
    TECHNOLOGY = "Technology"
    CONCEPT = "Concept"
    DOCUMENT = "Document"
    REPOSITORY = "Repository"
    SOURCE = "Source"
    DEVICE = "Device"
    DECISION = "Decision"
    REQUIREMENT = "Requirement"
    TASK = "Task"
    EXPERIMENT = "Experiment"
    DATASET = "Dataset"
    RESEARCH_FINDING = "ResearchFinding"
    EPISODE = "Episode"
    APPLICATION = "Application"
    INFRASTRUCTURE_COMPONENT = "InfrastructureComponent"


class RelationshipGroup(StrEnum):
    """How an edge was produced. ``STRUCTURAL`` edges exist after Tier 0/1 without any LLM."""

    STRUCTURAL = "structural"
    SEMANTIC = "semantic"
    TEMPORAL = "temporal"


class Predicate(StrEnum):
    """Every predicate a :class:`~aimemory.domain.models.Fact` may carry.

    The union of ``schemas/ontology.yaml: relationship_types`` (22 edge types projected into Neo4j)
    and ``functional_predicates`` (3 attribute-like predicates whose object is usually a literal and
    which are *not* Neo4j edge types). ``schemas/extraction/relationship_extraction.schema.json``
    enumerates the subset the LLM is allowed to emit.

    ADR-0015 moved ``HAS_OWNER``, ``USES_ARCHITECTURE`` and ``DEPLOYED_ON`` out of the functional
    group and into ``semantic``: a project uses several technologies and is owned by several people
    at once, so treating them as single-valued closed concurrently-true facts as ``historical``.
    Membership of the functional group now requires that two concurrent values be a *contradiction*.
    """

    # structural
    PART_OF = "PART_OF"
    BELONGS_TO = "BELONGS_TO"
    STORED_ON = "STORED_ON"
    HAS_SOURCE = "HAS_SOURCE"
    MENTIONS = "MENTIONS"
    LINKS_TO = "LINKS_TO"
    DERIVED_FROM = "DERIVED_FROM"
    # semantic
    USES = "USES"
    DEPENDS_ON = "DEPENDS_ON"
    RELATED_TO = "RELATED_TO"
    IMPLEMENTS = "IMPLEMENTS"
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    PRODUCES = "PRODUCES"
    REQUIRES = "REQUIRES"
    CREATED_BY = "CREATED_BY"
    GENERATED_BY = "GENERATED_BY"
    HAS_OWNER = "HAS_OWNER"
    USES_ARCHITECTURE = "USES_ARCHITECTURE"
    DEPLOYED_ON = "DEPLOYED_ON"
    # temporal
    SUPERSEDES = "SUPERSEDES"
    DECIDED_IN = "DECIDED_IN"
    # functional (attribute-like; single current object per subject - ADR-0005 rule 1, ADR-0015)
    HAS_STATUS = "HAS_STATUS"
    HAS_STAGE = "HAS_STAGE"
    SELECTED_OPTION = "SELECTED_OPTION"


class Track(StrEnum):
    """Mirrors ``schemas/ontology.yaml: tracks`` - the owner's four portfolio tracks."""

    BUSINESS = "business"
    RESEARCH = "research"
    CAREER = "career"
    FOUNDATION = "foundation"


class ProjectStatus(StrEnum):
    """Registry status seeded from ``AIOS/Maps/project-graph.md`` (Tier 0)."""

    ACTIVE = "active"
    PAUSED = "paused"
    PLANNED = "planned"
    COMPLETED = "completed"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class DocKind(StrEnum):
    """Document classification produced by call 1 of the native engine. Mirrors the ``doc_kind``
    enum of ``schemas/extraction/episode_extraction.schema.json``."""

    DECISION_RECORD = "decision_record"
    SPECIFICATION = "specification"
    DESIGN_DOC = "design_doc"
    NOTES = "notes"
    README = "readme"
    SOURCE_CODE = "source_code"
    TEST = "test"
    CONFIG = "config"
    TUTORIAL = "tutorial"
    ARTICLE = "article"
    CLIPPING = "clipping"
    REGISTRY = "registry"
    CAREER = "career"
    RESEARCH = "research"
    OTHER = "other"


class ObjectType(StrEnum):
    """Polymorphic target of ``embeddings``, ``retrieval_logs``, ``extraction_reviews`` and
    ``Gateway.explain(id)``."""

    CHUNK = "chunk"
    ARTIFACT = "artifact"
    FACT = "fact"
    ENTITY = "entity"
    EPISODE = "episode"
    SOURCE = "source"


class SourceEventType(StrEnum):
    """Append-only ``source_events`` (plan section G)."""

    CREATED = "created"
    MODIFIED = "modified"
    MOVED = "moved"
    DELETED = "deleted"
    RESTORED = "restored"
    POLICY_CHANGED = "policy_changed"
    SECRET_SUSPECTED = "secret_suspected"
    RESCANNED = "rescanned"


class RunStatus(StrEnum):
    """``ingestion_runs.status``."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(StrEnum):
    """Who started an ingestion run. ``SCHEDULE`` is reserved; AC-10 declined the scheduled task."""

    CLI = "cli"
    OPS_PAGE = "ops_page"
    WORKER = "worker"
    SCHEDULE = "schedule"
    TEST = "test"


class RunAction(StrEnum):
    """ADR-0011: the only actions the Ops page may enqueue. No Docker control, ever."""

    SCAN = "scan"
    RETRY_FAILED = "retry_failed"
    EVAL = "eval"
    BENCHMARK = "benchmark"


class RunRequestStatus(StrEnum):
    """ADR-0011 ``run_requests`` lifecycle, polled by ``aimemory-ingest worker``."""

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReviewVerdict(StrEnum):
    """ADR-0010 human review. Acceptance rate = ``ACCEPT / (ACCEPT + WRONG + PARTIAL)``."""

    ACCEPT = "accept"
    WRONG = "wrong"
    PARTIAL = "partial"


class RetrieverKind(StrEnum):
    """Which retriever produced a candidate before Reciprocal Rank Fusion (plan section P)."""

    SEMANTIC = "semantic"
    KEYWORD = "keyword"
    GRAPH = "graph"


class McpToolKind(StrEnum):
    """``schemas/mcp/tools.json``: 10 read tools, 2 write tools (ADR-0008)."""

    READ = "read"
    WRITE = "write"
