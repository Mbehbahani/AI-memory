"""Ports - the interfaces every implementation must satisfy. No behaviour lives here.

Consumers and their obligations:

======================================  ==========================================================
Port                                    Implemented by
======================================  ==========================================================
:class:`LLMProvider`                    A05 - ``providers/llm/ollama_provider.py`` (plan section M)
:class:`EmbeddingProvider`              A06 - ``providers/embedding/`` client for the MiniLM service
:class:`KnowledgeEngine`                A08 - ``knowledge/graphiti_engine`` or ``native_engine``
                                        depending on the ADR-0002 gate verdict (ADR-0009)
:class:`GraphStore`                     A04/A08 - ``persistence/graph_store.py`` + ``providers/graph``
:class:`TextExtractor`                  A07b - ``extractors/`` (markdown, pdf, docx, ipynb, code)
:class:`Chunker`                        A07b - ``chunking/``
======================================  ==========================================================

These are :class:`typing.Protocol` classes with ``@runtime_checkable``, not ABCs: an implementation
does not import the domain package to inherit from it, which keeps the dependency arrow pointing one
way (implementations -> contracts) and lets tests supply plain fakes. ``isinstance`` checks the
method names only; ``tests/unit/test_contracts.py`` additionally compares signatures.

The port boundary is what makes ADR-0002 possible: the Graphiti gate can swap the whole extraction
engine without the Gateway, MCP or the database layer changing a line.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import Field

from .base import DomainModel
from .enums import EngineKind, EntityType, Predicate
from .extraction import ExtractionResult
from .models import Chunk, EmbeddingModel, Episode, ExtractionModel

__all__ = [
    "ChunkDraft",
    "Chunker",
    "EmbeddingProvider",
    "EmbeddingResult",
    "EpisodeContext",
    "ExtractedText",
    "GraphNode",
    "GraphRelationship",
    "GraphStore",
    "KnowledgeEngine",
    "LLMProvider",
    "LLMResponse",
    "TextExtractor",
    "chunk_from_draft",
]


# --------------------------------------------------------------------------------------------------
# Port payloads
# --------------------------------------------------------------------------------------------------


class LLMResponse(DomainModel):
    """One completed LLM call. ``attempts`` counts schema-retry rounds (plan section M: max 2).

    ``parsed`` is the decoded JSON object when the call used ``format=<schema>``; ``text`` is always
    the raw content, kept for the failure record. Token counts and duration are MEASURED per call and
    feed the ADR-0010 metrics; they are ``None`` when the provider does not report them.
    """

    text: str
    parsed: dict[str, Any] | None = None
    model: str
    model_digest: str | None = None
    attempts: int = Field(default=1, ge=1)
    duration_ms: int = Field(default=0, ge=0)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    valid: bool = True
    errors: list[str] = Field(default_factory=list)


class EmbeddingResult(DomainModel):
    """A batch of vectors with the identity of the model that produced them."""

    vectors: list[list[float]]
    model_id: str
    dimension: int = Field(ge=1)
    duration_ms: int = Field(default=0, ge=0)


class EpisodeContext(DomainModel):
    """Everything the engine may know about an episode besides its text.

    Deliberately *not* the whole database: the engine gets the project, the known entity names for
    deterministic-first resolution, the previous version's facts (so ADR-0005 rule 3 can mark
    unconfirmed), and the ontology it must stay inside. Anything else is the caller's job.
    """

    project_id: str | None = None
    source_uri: str | None = None
    doc_title: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    observed_at: datetime | None = None
    known_entity_names: list[str] = Field(default_factory=list)
    previous_fact_ids: list[UUID] = Field(default_factory=list)
    allowed_entity_types: list[EntityType] = Field(default_factory=list)
    allowed_predicates: list[Predicate] = Field(default_factory=list)
    extra: dict[str, str] = Field(default_factory=dict)


class ExtractedText(DomainModel):
    """What a :class:`TextExtractor` returns for one source version.

    ``ok=False`` with a ``reason`` is a *normal* outcome, not an exception: plan section P6-T02 says a
    pdf/docx failure downgrades the source to CATALOG_ONLY with a recorded reason rather than failing
    the run.
    """

    text: str = ""
    extractor: str
    extractor_version: str = "0.1.0"
    media_type: str | None = None
    char_count: int = Field(default=0, ge=0)
    truncated: bool = False
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    links: list[str] = Field(default_factory=list)
    ok: bool = True
    reason: str | None = None


class ChunkDraft(DomainModel):
    """A chunk before it gets an id and a version (what a :class:`Chunker` yields).

    ``token_count`` must respect the embedding model's ``max_seq`` (256 for MiniLM-L6-v2, so chunks
    target ~200 tokens); A07b owns the splitting strategy, this only fixes the shape.
    """

    ordinal: int = Field(ge=0)
    text: str
    text_hash: str
    heading_path: list[str] = Field(default_factory=list)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    token_count: int = Field(ge=0)


class GraphNode(DomainModel):
    """A node as written to Neo4j. ``properties`` must include the plan section H property set.

    ``label`` is the *stored* label: a ``SubProject`` entity is written as ``Project`` with
    ``parent_id`` set (see :class:`~aimemory.domain.enums.EntityType`).
    """

    id: UUID | str
    label: EntityType
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphRelationship(DomainModel):
    """An edge as written to Neo4j, carrying the fact identity and its validity window."""

    fact_id: UUID | str
    predicate: Predicate
    from_id: UUID | str
    to_id: UUID | str
    properties: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------------------
# Ports
# --------------------------------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """Schema-constrained local text generation (plan section M).

    Implementation contract (A05): ``/api/chat`` with ``format=<json schema>``, ``think=false``,
    ``temperature=0``, ``num_ctx`` and ``num_predict`` from settings, a request timeout, and at most
    ``max_retries`` retries that feed the validation error back to the model. Never falls back to a
    cloud model - if Qwen3 4B cannot do the job, the evidence is recorded and development stops
    (CLAUDE.md execution rules).
    """

    def complete_json(
        self,
        prompt: str,
        json_schema: dict[str, Any],
        *,
        system: str | None = None,
        max_retries: int | None = None,
    ) -> LLMResponse:
        """Generate a JSON object constrained by ``json_schema``. Retries on invalid output."""
        ...

    def complete_text(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        """Unconstrained generation. Used only for summaries; never for structured knowledge."""
        ...

    def model_identity(self) -> ExtractionModel:
        """Model name + digest + effective parameters, stamped on every artifact it produces."""
        ...

    def health(self) -> bool:
        """True when the model is reachable and loadable. Used by ``/health`` and the Ops page."""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """384-dimensional normalized sentence embeddings (plan section N).

    The same port covers the in-process sentence-transformers model inside apps/embedding-service and
    the HTTP client used by ingestion and the Gateway, so a swap to text-embeddings-inference is a
    configuration change.
    """

    @property
    def dimensions(self) -> int:
        """Vector width. Must equal ``embeddings.vector(n)`` in Postgres."""
        ...

    def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        """Embed a batch. Order of the returned vectors matches the order of ``texts``."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed one query string. Separated so a future asymmetric model can differ here."""
        ...

    def model_identity(self) -> EmbeddingModel:
        """Name, dimension, revision, normalization - written to ``embedding_models``."""
        ...

    def health(self) -> bool:
        ...


@runtime_checkable
class KnowledgeEngine(Protocol):
    """The ADR-0002 seam: turns an episode into knowledge, however it does so internally.

    Two implementations exist (A08): ``GraphitiEngine`` wrapping ``graphiti-core``, and
    ``NativeTemporalEngine`` using our prompts plus ``schemas/extraction/*.json``. The P4 gate picks
    one; ADR-0009 records the verdict. Both must:

    * return the same :class:`~aimemory.domain.extraction.ExtractionResult` shape;
    * write nothing to Postgres themselves - the caller persists, so ADR-0001 (Postgres is the system
      of record) holds for either engine;
    * stay inside ``schemas/ontology.yaml`` for labels and predicates;
    * be safely callable one episode at a time (``INGEST_LLM_CONCURRENCY=1``).
    """

    @property
    def kind(self) -> EngineKind:
        """``native`` or ``graphiti``; stamped on every row the result produces."""
        ...

    def process_episode(self, episode: Episode, context: EpisodeContext) -> ExtractionResult:
        """Extract entities, artifacts and facts from one episode.

        Must not raise for model misbehaviour: an invalid response after retries returns a result
        with ``valid=False`` and sanitized ``errors``, so the episode can be marked ``failed`` and
        reprocessed later without losing the Tier 1 vectors.
        """
        ...

    def invalidate(self, fact_id: UUID, at: datetime, by_episode: UUID | None = None) -> None:
        """Close a fact at ``at`` (ADR-0005): set ``valid_to``, mark it ``historical``, record who.

        Never deletes. Idempotent: invalidating an already-closed fact at the same instant is a no-op.
        """
        ...


@runtime_checkable
class GraphStore(Protocol):
    """Neo4j projection (ADR-0001: rebuildable, never the sole holder of a fact).

    All writes are idempotent MERGEs keyed on the Postgres id, so ``scripts/rebuild-graph`` can
    replay the whole projection from Postgres at any time. The Gateway uses a read-only user and only
    ever calls :meth:`query` / :meth:`neighbours`.
    """

    def apply_schema(self, statements: Sequence[str]) -> None:
        """Apply ``infra/neo4j/schema/constraints.cypher``. Idempotent (``IF NOT EXISTS``)."""
        ...

    def upsert_nodes(self, nodes: Sequence[GraphNode]) -> int:
        """MERGE nodes on ``(label, id)``; returns the number written."""
        ...

    def upsert_relationships(self, relationships: Sequence[GraphRelationship]) -> int:
        """MERGE edges on ``fact_id``; returns the number written."""
        ...

    def invalidate_relationship(self, fact_id: UUID | str, at: datetime) -> bool:
        """Set ``valid_to`` on the edge with this ``fact_id``. Returns False if it was not there."""
        ...

    def neighbours(
        self,
        entity_ids: Sequence[UUID | str],
        *,
        predicates: Sequence[Predicate] | None = None,
        depth: int = 1,
        limit: int = 25,
        as_of: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """1-hop (max 2) expansion used by retrieval step 3, bounded by ``graph_expansion`` config."""
        ...

    def query(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a read query. Implementations must refuse write clauses on a read-only session."""
        ...

    def clear(self) -> None:
        """Delete every projected node/edge. Only ``scripts/rebuild-graph`` may call this."""
        ...

    def health(self) -> bool:
        ...


@runtime_checkable
class TextExtractor(Protocol):
    """Bytes/paths to plain text for one media type family (A07b).

    A failure is reported through :class:`ExtractedText` (``ok=False``), not raised, so one broken
    PDF never fails a scan of 1,700 files.
    """

    @property
    def name(self) -> str:
        """Value stored in ``source_text.extractor``."""
        ...

    def supports(self, relative_path: str, media_type: str | None = None) -> bool:
        """True when this extractor claims the file."""
        ...

    def extract(self, data: bytes, *, relative_path: str, max_bytes: int | None = None) -> ExtractedText:
        """Extract text. ``max_bytes`` is ``INGEST_MAX_TEXT_BYTES``; exceeding it sets ``truncated``."""
        ...


@runtime_checkable
class Chunker(Protocol):
    """Text to retrievable chunks (A07b).

    Chunks are heading-aware for markdown and structure-aware for code; every chunk carries its
    ``heading_path`` and character offsets, because those are part of the provenance stamp and of the
    citation string.
    """

    @property
    def name(self) -> str:
        ...

    def chunk(self, text: str, *, relative_path: str, max_tokens: int = 200) -> list[ChunkDraft]:
        """Split ``text``. Offsets must be valid indexes into the *input* string."""
        ...


def chunk_from_draft(
    draft: ChunkDraft,
    *,
    chunk_id: UUID,
    version_id: UUID,
    source_id: UUID,
    project_id: str | None = None,
) -> Chunk:
    """Promote a :class:`ChunkDraft` to a persistable :class:`~aimemory.domain.models.Chunk`.

    Lives here because it is the only place the two shapes meet; A07a calls it after fingerprinting.
    """
    return Chunk(
        id=chunk_id,
        version_id=version_id,
        source_id=source_id,
        project_id=project_id,
        ordinal=draft.ordinal,
        text=draft.text,
        text_hash=draft.text_hash,
        heading_path=draft.heading_path,
        char_start=draft.char_start,
        char_end=draft.char_end,
        token_count=draft.token_count,
    )

