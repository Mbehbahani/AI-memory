"""Provenance - the exact ``[PROV]`` column set of plan section J.

Consumers: A04 (the ``[PROV]`` columns exist on ``entity_mentions``, ``facts`` and
``knowledge_artifacts``, and the ``provenance_v`` view projects exactly these names), A08 (builds a
:class:`Provenance` for every derived row), A09 (``/v1/explain/{id}``, citations, context blocks),
A10 (``memory.get_sources`` / ``memory.explain``), A12 (the "provenance completeness 100 %" check of
plan section Y).

The contract, quoted from the plan:

    Every derived row: ``source_id, source_uri, source_hash, source_version, project_id, device_id,
    observed_at, valid_from, valid_to, confidence, extraction_model_id, embedding_model_id,
    ingestion_run_id, episode_id`` (+ ``heading_path``/offsets for chunks).

:data:`PROVENANCE_COLUMNS` is that list, in that order, and is asserted against the model by
``tests/unit/test_contracts.py`` so the SQL and the Python can never drift.

Registry-derived facts (Tier 0, no LLM) carry
``extraction_model_id = "deterministic:registry-v1"`` (:data:`DETERMINISTIC_MODEL_ID`).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from .base import DomainModel

__all__ = [
    "DETERMINISTIC_MODEL_ID",
    "PROVENANCE_COLUMNS",
    "Provenance",
    "ProvenanceChainStep",
]

#: Ordered ``[PROV]`` column set (plan section J). Do not reorder without an ADR.
PROVENANCE_COLUMNS: tuple[str, ...] = (
    "source_id",
    "source_uri",
    "source_hash",
    "source_version",
    "project_id",
    "device_id",
    "observed_at",
    "valid_from",
    "valid_to",
    "confidence",
    "extraction_model_id",
    "embedding_model_id",
    "ingestion_run_id",
    "episode_id",
)

#: Stamped on rows produced without an LLM (Tier 0 registry seed, structural graph).
DETERMINISTIC_MODEL_ID = "deterministic:registry-v1"


class Provenance(DomainModel):
    """The provenance stamp carried by every derived row.

    ``source_version`` is the ``source_versions.id`` the row was derived from - the plan's column
    name is kept verbatim even though it holds a version *id*, because A04's DDL, the
    ``provenance_v`` view and the MCP payload all use that name.

    The three chunk-only fields (``heading_path``, ``char_start``, ``char_end``) are optional and set
    only when the provenance points at a chunk.
    """

    source_id: UUID | None = Field(default=None, description="sources.id")
    source_uri: str | None = Field(default=None, description="Logical URI (ADR-0004)")
    source_hash: str | None = Field(default=None, description="sha256: content hash of the version")
    source_version: UUID | None = Field(default=None, description="source_versions.id")
    project_id: str | None = Field(default=None, description="projects.id slug")
    device_id: str = Field(description="devices.id; 'local-development-machine' in V0.1")
    observed_at: datetime = Field(description="Source time (mtime/commit) or run time (ADR-0005 #6)")
    valid_from: datetime | None = Field(default=None)
    valid_to: datetime | None = Field(default=None, description="NULL = still current")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    extraction_model_id: str | None = Field(
        default=None, description="extraction_models.id, or 'deterministic:registry-v1'"
    )
    embedding_model_id: str | None = Field(default=None, description="embedding_models.id")
    ingestion_run_id: UUID | None = Field(default=None)
    episode_id: UUID | None = Field(default=None)

    # chunk-only positional detail (plan section J: "+ heading_path/offsets for chunks")
    heading_path: list[str] = Field(default_factory=list)
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)

    @property
    def is_complete(self) -> bool:
        """True when the chain can be walked back to a source version.

        Used by the plan section Y "provenance completeness" metric. Manual/MCP episodes have no
        source, so completeness is measured over source-derived rows only.
        """
        return self.source_id is not None and self.source_version is not None

    def citation(self) -> str:
        """Render the ``config/retrieval.yaml: context.citation_format`` string."""
        from ..common.hashing import short_hash  # local import keeps domain import-cycle free

        heading = " > ".join(self.heading_path) if self.heading_path else ""
        digest = short_hash(self.source_hash) if self.source_hash else "unknown"
        return f"[{self.source_uri or 'unknown'}#{heading} @{digest}]"


class ProvenanceChainStep(DomainModel):
    """One hop of the human-readable chain returned by ``Gateway.explain(id)``.

    The chain is ordered ``artifact|fact -> episode -> version -> source -> root -> device`` with the
    models and the run appended, exactly as plan section J describes.
    """

    kind: str = Field(description="artifact | fact | entity | episode | version | source | root "
                                 "| device | model | run")
    id: str = Field(description="Identifier of the object at this hop (UUID or slug)")
    label: str = Field(description="Human-readable label shown in explain output")
    detail: dict[str, str] = Field(default_factory=dict)
