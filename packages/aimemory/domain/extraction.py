"""Extraction DTOs - the Python mirror of ``schemas/extraction/*.json``.

Consumers: A05 (``OllamaProvider`` passes the JSON Schema as Ollama ``format=`` and parses the reply
into these models), A08 (both the native engine and the Graphiti adapter return
:class:`ExtractionResult`), A12 (``tests/unit/test_contracts.py`` asserts field-for-field equivalence
between these classes and the JSON Schema files, so the two can never drift).

Layering:

* :class:`EpisodeExtraction` and :class:`RelationshipExtraction` are *raw model output* - names, not
  ids; strings, not resolved entities; ``*_if_stated`` fields that may be ``None``. They use
  ``extra="forbid"`` because the schema is passed to the decoder with ``additionalProperties:false``.
* :class:`ExtractionResult` is the *engine port return value*: still pre-persistence, but with the
  episode it belongs to, the engine/model identity, timing and validation outcome attached. It is
  the single value :meth:`~aimemory.domain.ports.KnowledgeEngine.process_episode` returns, and it is
  identical whether Graphiti or the native engine produced it (ADR-0002).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field, field_validator

from .base import DomainModel
from .enums import (
    ArtifactType,
    DocKind,
    EngineKind,
    EntityType,
    Predicate,
    StatedArtifactStatus,
)

__all__ = [
    "EPISODE_EXTRACTION_SCHEMA_ID",
    "EXTRACTABLE_ARTIFACT_TYPES",
    "EXTRACTABLE_ENTITY_TYPES",
    "EXTRACTABLE_PREDICATES",
    "RELATIONSHIP_EXTRACTION_SCHEMA_ID",
    "EpisodeExtraction",
    "ExtractedArtifact",
    "ExtractedEntity",
    "ExtractedFact",
    "ExtractionResult",
    "RelationshipExtraction",
]

#: ``$id`` of ``schemas/extraction/episode_extraction.schema.json``.
EPISODE_EXTRACTION_SCHEMA_ID = "aimemory/extraction/episode_extraction/v0.1"
#: ``$id`` of ``schemas/extraction/relationship_extraction.schema.json``.
RELATIONSHIP_EXTRACTION_SCHEMA_ID = "aimemory/extraction/relationship_extraction/v0.1"

#: Entity labels the model may emit. ``Source``, ``Device`` and ``Episode`` are excluded: those nodes
#: are created deterministically from the registry and the ingestion run, never guessed.
EXTRACTABLE_ENTITY_TYPES: tuple[EntityType, ...] = tuple(
    t for t in EntityType if t not in (EntityType.SOURCE, EntityType.DEVICE, EntityType.EPISODE)
)

#: Artifact types the model may emit. ``summary`` is written by the engine from ``EpisodeExtraction.
#: summary``, not chosen by the model.
EXTRACTABLE_ARTIFACT_TYPES: tuple[ArtifactType, ...] = tuple(
    t for t in ArtifactType if t is not ArtifactType.SUMMARY
)

#: Predicates the model may emit. The six omitted ones (``STORED_ON``, ``HAS_SOURCE``, ``MENTIONS``,
#: ``LINKS_TO``, ``DERIVED_FROM``, ``DECIDED_IN``) are produced deterministically by the structural
#: projection, so letting the model invent them would create unverifiable provenance edges.
EXTRACTABLE_PREDICATES: tuple[Predicate, ...] = tuple(
    p
    for p in Predicate
    if p
    not in (
        Predicate.STORED_ON,
        Predicate.HAS_SOURCE,
        Predicate.MENTIONS,
        Predicate.LINKS_TO,
        Predicate.DERIVED_FROM,
        Predicate.DECIDED_IN,
    )
)


class ExtractedEntity(DomainModel):
    """One entity as emitted by call 1. ``type`` is an ontology label; ``Source``, ``Device`` and
    ``Episode`` are deliberately not offered to the model - those nodes are deterministic."""

    name: str = Field(min_length=1, max_length=120)
    type: EntityType
    aliases: list[str] = Field(default_factory=list, max_length=5)
    description: str | None = Field(default=None, max_length=300)

    @field_validator("type")
    @classmethod
    def _extractable(cls, value: EntityType) -> EntityType:
        if value not in EXTRACTABLE_ENTITY_TYPES:
            raise ValueError(f"{value} is produced deterministically, not by the extraction model")
        return value


class ExtractedArtifact(DomainModel):
    """One knowledge artifact as emitted by call 1.

    ``supersedes_if_stated`` carries ADR-0005 rule 2: explicit supersession written in the text is
    authoritative over any inference the temporal engine would otherwise make. ``evidence_quote`` is
    what the weekly review (ADR-0010) shows the owner next to the citation.
    """

    artifact_type: ArtifactType
    title: str = Field(min_length=3, max_length=160)
    statement: str = Field(min_length=3, max_length=1200)
    status: StatedArtifactStatus | None = None
    date_if_stated: str | None = Field(
        default=None, description="ISO-8601 date if the text states it; otherwise null"
    )
    supersedes_if_stated: str | None = Field(
        default=None, description="Title of the earlier artifact this replaces, only if stated"
    )
    related_entities: list[str] = Field(default_factory=list, max_length=10)
    evidence_quote: str | None = Field(default=None, max_length=300)

    @field_validator("artifact_type")
    @classmethod
    def _extractable(cls, value: ArtifactType) -> ArtifactType:
        if value not in EXTRACTABLE_ARTIFACT_TYPES:
            raise ValueError(f"{value} is written by the engine, not chosen by the model")
        return value


class EpisodeExtraction(DomainModel):
    """Call 1 result: classification + entities + artifacts + summary for one episode."""

    doc_kind: DocKind
    summary: str = Field(max_length=600)
    entities: list[ExtractedEntity] = Field(default_factory=list, max_length=40)
    artifacts: list[ExtractedArtifact] = Field(default_factory=list, max_length=30)


class ExtractedFact(DomainModel):
    """One fact as emitted by call 2. ``subject``/``object`` must be names from the call-1 entity
    list; the engine resolves them to entity ids before writing, and drops unresolvable triples."""

    subject: str = Field(min_length=1, max_length=120)
    predicate: Predicate
    object: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=3, max_length=400)
    valid_from_if_stated: str | None = None
    valid_to_if_stated: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("predicate")
    @classmethod
    def _extractable(cls, value: Predicate) -> Predicate:
        if value not in EXTRACTABLE_PREDICATES:
            raise ValueError(f"{value} is a structural predicate produced without an LLM")
        return value


class RelationshipExtraction(DomainModel):
    """Call 2 result: facts between the entities already extracted in call 1."""

    facts: list[ExtractedFact] = Field(default_factory=list, max_length=60)


class ExtractionResult(DomainModel):
    """What :meth:`~aimemory.domain.ports.KnowledgeEngine.process_episode` returns.

    Engine-agnostic by contract (ADR-0002): the Gateway and MCP never learn which engine ran. The
    engine fills ``entities``/``artifacts``/``facts`` with *pre-persistence* values; A08 then resolves
    entities, applies the ADR-0005 temporal rules, writes Postgres and projects Neo4j.

    ``valid`` is false when the model output failed schema validation after the configured retries;
    in that case ``errors`` explains why (sanitized), the episode is marked ``failed``, and the Tier 1
    vectors are untouched. ``attempts`` and ``latency_ms`` feed the ADR-0010 metrics.
    """

    episode_id: UUID
    engine: EngineKind
    extraction_model_id: str
    doc_kind: DocKind | None = None
    summary: str | None = None
    entities: list[ExtractedEntity] = Field(default_factory=list)
    artifacts: list[ExtractedArtifact] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    graph_episode_uuid: str | None = Field(
        default=None, description="Graphiti's own episode id when that engine ran"
    )
    valid: bool = True
    attempts: int = Field(default=1, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list, description="Sanitized messages only")
    started_at: datetime | None = None
    finished_at: datetime | None = None
