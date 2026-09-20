"""Retrieval DTOs - the request/response contract of the Memory Gateway (plan sections P and Q).

Consumers: A09 (implements the pipeline and the REST layer against exactly these types),
A10 (``memory.search`` and friends serialize them for MCP), A12 (gold-set evaluation reads
:class:`ScoredHit` and :class:`AssembledContext`), A16 (Ops page shows retrieval metrics).

Pipeline shape (each stage's output is the next stage's input):

    SearchQuery
      -> semantic Candidate[]  (pgvector HNSW, top ``candidates.semantic_top_k``)
      -> keyword  Candidate[]  (tsvector,      top ``candidates.keyword_top_k``)
      -> RRF fusion (``fusion.rrf_k``)      -> ScoredHit[] (``candidates.fused_top_k``)
      -> graph expansion (Neo4j, 1 hop)     -> ScoredHit[] + related entities
      -> temporal filter (``as_of``)        -> ScoredHit[]
      -> boosts + final ranking             -> ScoredHit[] (``candidates.final_k``)
      -> context assembly (``context.token_budget``) -> AssembledContext
      -> SearchResult

All tuning constants come from ``config/retrieval.yaml``; :class:`RetrievalConfig` is the typed view
of that file so A09 never reads raw YAML keys.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, model_validator

from .base import DomainModel
from .enums import ArtifactType, EntityType, FactStatus, ObjectType, RetrieverKind
from .provenance import Provenance, ProvenanceChainStep

__all__ = [
    "AssembledContext",
    "Candidate",
    "ContextBlock",
    "ExplainChain",
    "RelatedEntity",
    "RetrievalConfig",
    "ScoredHit",
    "SearchQuery",
    "SearchResult",
]


class SearchQuery(DomainModel):
    """A scoped, time-aware retrieval request.

    ``project_ids`` scopes to one or more registry slugs (empty = all projects). ``as_of`` applies the
    ADR-0005 point-in-time predicate to facts and artifacts, **and selects which version of each
    file's text is visible** - a search sees exactly one version per source, the newest observed at or
    before ``as_of`` (``retrieval.filters.CURRENT_VERSION_PREDICATE``). Before that predicate existed,
    every version of an edited file stayed searchable at once and a hit could quote text no longer in
    the file. ``since`` filters on ``observed_at``/version time and answers "what changed lately".
    ``limit`` is the number of hits returned (``candidates.final_k`` is the default), not the number
    of candidates retrieved.

    ``include_unconfirmed`` defaults to true because ADR-0005 rule 3 keeps unconfirmed facts
    *current*; they are penalised by ``boosts.unconfirmed_penalty`` and flagged in the context rather
    than hidden (risk R6).
    """

    query: str = Field(min_length=1)
    project_ids: list[str] = Field(default_factory=list)
    object_types: list[ObjectType] = Field(
        default_factory=list, description="Empty = chunks + artifacts"
    )
    entity_types: list[EntityType] = Field(default_factory=list)
    artifact_types: list[ArtifactType] = Field(default_factory=list)
    as_of: datetime | None = None
    since: datetime | None = None
    limit: int = Field(default=10, ge=1, le=100, alias="k")
    expand: bool = Field(default=True, description="Graph expansion; false = vector+keyword only")
    include_unconfirmed: bool = True
    include_deleted_sources: bool = Field(
        default=False, description="Sources with status=deleted; knowledge is kept but flagged"
    )
    assemble_context: bool = True
    client: str | None = Field(default=None, description="MCP client id or 'rest'")


class Candidate(DomainModel):
    """A pre-fusion hit from one retriever. ``rank`` is 1-based within that retriever's own list -
    Reciprocal Rank Fusion uses the rank, not the raw score, which is why both are carried."""

    object_type: ObjectType
    object_id: UUID
    retriever: RetrieverKind
    rank: int = Field(ge=1)
    raw_score: float = Field(description="Cosine similarity, ts_rank, or graph proximity")
    project_id: str | None = None
    source_id: UUID | None = None
    text: str | None = None


class ScoredHit(DomainModel):
    """A fused, boosted, temporally filtered hit - the unit returned to callers.

    ``score = rrf_score + sum(boosts.values())`` with the weights of ``config/retrieval.yaml``
    (``project_match``, ``entity_linked``, recency decay, ``unconfirmed_penalty``). ``boosts`` keeps
    each contribution separately so a ranking regression can be explained instead of guessed at.

    ``provenance`` is always populated: the plan's acceptance criterion is provenance completeness
    100 % on returned evidence.
    """

    object_type: ObjectType
    object_id: UUID
    title: str | None = None
    text: str
    score: float
    rrf_score: float = 0.0
    boosts: dict[str, float] = Field(default_factory=dict)
    retrievers: list[RetrieverKind] = Field(default_factory=list)
    rank: int = Field(default=0, ge=0)
    project_id: str | None = None
    entity_ids: list[UUID] = Field(default_factory=list)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    status: FactStatus | str | None = Field(
        default=None, description="Fact/artifact status; surfaced so 'unconfirmed' is visible"
    )
    provenance: Provenance
    citation: str = Field(default="", description="config/retrieval.yaml context.citation_format")


class RelatedEntity(DomainModel):
    """An entity reached by 1-hop graph expansion, with the edge that got us there."""

    entity_id: UUID
    name: str
    type: EntityType
    predicate: str
    direction: str = Field(default="out", description="out | in")
    project_id: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ContextBlock(DomainModel):
    """One ordered section of the assembled context.

    ``kind`` is a member of ``config/retrieval.yaml: context.block_order``
    (``project_summary``, ``current_facts``, ``decisions``, ``evidence_chunks``,
    ``related_entities``). Every block carries its own citations so the text handed to a future AI
    client is never uncited.
    """

    kind: str
    title: str
    text: str
    tokens: int = Field(ge=0)
    citations: list[str] = Field(default_factory=list)
    object_ids: list[UUID] = Field(default_factory=list)
    flags: list[str] = Field(
        default_factory=list, description="e.g. 'unconfirmed', 'source-deleted', 'low-trust'"
    )


class AssembledContext(DomainModel):
    """The budgeted, ordered context text (plan section P step 6).

    ``truncated`` is true when ``token_budget`` cut blocks; ``dropped_blocks`` names what was cut, so
    an answer can never silently lose the decisions section.
    """

    blocks: list[ContextBlock] = Field(default_factory=list)
    token_budget: int = Field(default=6000, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    truncated: bool = False
    dropped_blocks: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _budget_respected(self) -> AssembledContext:
        if self.tokens_used > self.token_budget:
            raise ValueError("tokens_used exceeds token_budget")
        return self


class SearchResult(DomainModel):
    """The Gateway's answer to a :class:`SearchQuery` (``/v1/search`` and ``memory.search``).

    ``warnings`` carries degraded-mode notices - the plan's failure tests require "Neo4j down ->
    vector-only with warning", and that warning must reach the caller, not just the log.
    """

    query: SearchQuery
    hits: list[ScoredHit] = Field(default_factory=list)
    related_entities: list[RelatedEntity] = Field(default_factory=list)
    context: AssembledContext | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    candidate_counts: dict[str, int] = Field(default_factory=dict)
    latency_ms: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    retrieval_log_id: UUID | None = None


class ExplainChain(DomainModel):
    """``Gateway.explain(id)`` / ``memory.explain`` (plan section J).

    The ordered chain ``artifact|fact -> episode -> version -> source -> root/device`` plus the
    models and the run, with a plain-language rendering for humans. Never returns file bytes.
    """

    object_type: ObjectType
    object_id: UUID
    provenance: Provenance
    steps: list[ProvenanceChainStep] = Field(default_factory=list)
    evidence_quote: str | None = None
    citation: str | None = None
    explanation: str = Field(default="", description="Human-readable sentence chain")
    complete: bool = Field(default=False, description="Provenance.is_complete for this object")


class RetrievalConfig(DomainModel):
    """Typed view of ``config/retrieval.yaml``. Loaded once by A09; keys match the file exactly.

    Nested as flat attributes with the YAML path in each description, so a config rename is a typed
    failure instead of a silent default.
    """

    version: str = "0.1.0"
    semantic_top_k: int = Field(default=40, ge=1, description="candidates.semantic_top_k")
    keyword_top_k: int = Field(default=40, ge=1, description="candidates.keyword_top_k")
    fused_top_k: int = Field(default=15, ge=1, description="candidates.fused_top_k")
    final_k: int = Field(default=10, ge=1, description="candidates.final_k")
    fusion_method: str = Field(default="rrf", description="fusion.method")
    rrf_k: int = Field(default=60, ge=1, description="fusion.rrf_k")
    boost_project_match: float = Field(default=0.10, description="boosts.project_match")
    boost_entity_linked: float = Field(default=0.10, description="boosts.entity_linked")
    unconfirmed_penalty: float = Field(default=-0.15, description="boosts.unconfirmed_penalty")
    low_trust_penalty: float = Field(
        default=-0.15,
        description="boosts.low_trust_penalty (optional key; AC-6 clippings). Defaults to the "
        "unconfirmed penalty magnitude when the key is absent from config/retrieval.yaml.",
    )
    recency_half_life_days: float = Field(
        default=180.0, gt=0, description="boosts.recency_half_life_days"
    )
    graph_expansion_enabled: bool = Field(default=True, description="graph_expansion.enabled")
    graph_max_depth: int = Field(default=1, ge=1, le=2, description="graph_expansion.max_depth")
    graph_max_nodes: int = Field(default=25, ge=1, description="graph_expansion.max_nodes")
    graph_relationship_types: list[str] = Field(
        default_factory=list, description="graph_expansion.relationship_types"
    )
    token_budget: int = Field(default=6000, ge=1, description="context.token_budget")
    block_order: list[str] = Field(default_factory=list, description="context.block_order")
    citation_format: str = Field(
        default="[{source_uri}#{heading} @{hash8}]", description="context.citation_format"
    )
