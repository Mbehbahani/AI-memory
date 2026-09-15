"""Provenance builders - plan section J, in the frozen ``PROVENANCE_COLUMNS`` order (A08).

Every derived row in the system (``facts``, ``knowledge_artifacts``, ``entity_mentions``) carries the
same fourteen columns. Constructing that stamp by hand at each call site is how a column goes missing,
so it is constructed exactly once here, from the four things actually known at extraction time:

* the **episode** being processed (``episode_id``, ``observed_at``, ``project_id``),
* the **source version** it came from (``source_id``, ``source_uri``, ``source_hash``,
  ``source_version``) - absent for ``manual``/``mcp`` episodes, which have no file,
* the **run** that is executing (``ingestion_run_id``, ``device_id``),
* the **models** (``extraction_model_id``, ``embedding_model_id``).

:class:`EpisodeProvenance` holds those once per episode and mints a
:class:`~aimemory.domain.provenance.Provenance` per row with only the per-row parts
(``valid_from`` / ``valid_to`` / ``confidence``, plus chunk offsets for a mention).

ADR-0014 note: ``extraction_model_id`` is **load-bearing**, not informational. The corpus-level guard
A07a enforces can only be true if every write stamps the model that actually produced it, and a
re-extraction can only supersede a previous generation cleanly if the two generations are
distinguishable. :meth:`EpisodeProvenance.with_model` and :func:`deterministic_provenance` are the
only two supported ways to set it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from ..common.time import ensure_utc
from ..domain.models import Episode, Source, SourceVersion
from ..domain.provenance import (
    DETERMINISTIC_MODEL_ID,
    PROVENANCE_COLUMNS,
    Provenance,
)

__all__ = [
    "DETERMINISTIC_MODEL_ID",
    "PROVENANCE_COLUMNS",
    "EpisodeProvenance",
    "build_provenance",
    "deterministic_provenance",
    "missing_provenance_columns",
    "provenance_payload",
]

#: Columns a *source-derived* row must carry for the plan section Y completeness metric. Manual and
#: MCP episodes have no file, so completeness is measured over source-derived rows only.
SOURCE_DERIVED_REQUIRED: tuple[str, ...] = (
    "source_id",
    "source_uri",
    "source_hash",
    "source_version",
    "device_id",
    "observed_at",
    "valid_from",
    "extraction_model_id",
    "episode_id",
)


def build_provenance(
    *,
    device_id: str,
    observed_at: datetime,
    source_id: UUID | None = None,
    source_uri: str | None = None,
    source_hash: str | None = None,
    source_version: UUID | None = None,
    project_id: str | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    confidence: float = 1.0,
    extraction_model_id: str | None = None,
    embedding_model_id: str | None = None,
    ingestion_run_id: UUID | None = None,
    episode_id: UUID | None = None,
    heading_path: Sequence[str] = (),
    char_start: int | None = None,
    char_end: int | None = None,
) -> Provenance:
    """The one constructor. Keyword-only, so a column can never be filled by position."""
    return Provenance(
        source_id=source_id,
        source_uri=source_uri,
        source_hash=source_hash,
        source_version=source_version,
        project_id=project_id,
        device_id=device_id,
        observed_at=ensure_utc(observed_at),
        valid_from=ensure_utc(valid_from) if valid_from is not None else None,
        valid_to=ensure_utc(valid_to) if valid_to is not None else None,
        confidence=confidence,
        extraction_model_id=extraction_model_id,
        embedding_model_id=embedding_model_id,
        ingestion_run_id=ingestion_run_id,
        episode_id=episode_id,
        heading_path=list(heading_path),
        char_start=char_start,
        char_end=char_end,
    )


def deterministic_provenance(*, device_id: str, observed_at: datetime, **kwargs: Any) -> Provenance:
    """A stamp for a row produced without an LLM (Tier 0 seed, structural projection).

    ``extraction_model_id`` is forced to ``deterministic:registry-v1`` (plan section J) - a caller
    cannot accidentally attribute a deterministic row to a model.
    """
    kwargs.pop("extraction_model_id", None)
    return build_provenance(
        device_id=device_id,
        observed_at=observed_at,
        extraction_model_id=DETERMINISTIC_MODEL_ID,
        **kwargs,
    )


def provenance_payload(provenance: Provenance) -> dict[str, Any]:
    """The fourteen ``[PROV]`` values as a dict **in ``PROVENANCE_COLUMNS`` order**.

    Python dicts keep insertion order, so this is the canonical serialization used by the tests, the
    ``explain`` payload and anything that renders the stamp.
    """
    return {column: getattr(provenance, column) for column in PROVENANCE_COLUMNS}


def missing_provenance_columns(
    provenance: Provenance, *, required: Sequence[str] | None = None
) -> list[str]:
    """Which required ``[PROV]`` columns are unset, reported in contract order."""
    wanted = set(required) if required is not None else set(SOURCE_DERIVED_REQUIRED)
    ordered = [c for c in PROVENANCE_COLUMNS if c in wanted]
    return [column for column in ordered if getattr(provenance, column, None) is None]


@dataclass(frozen=True, slots=True)
class EpisodeProvenance:
    """Everything constant for one episode; mints the per-row stamps.

    Built once by the knowledge engine's persistence step and reused for every entity mention, fact
    and artifact that episode produces, which is what makes "provenance completeness = 100 %" a
    property of the code rather than of the care taken at each call site.
    """

    device_id: str
    observed_at: datetime
    episode_id: UUID | None = None
    project_id: str | None = None
    source_id: UUID | None = None
    source_uri: str | None = None
    source_hash: str | None = None
    source_version: UUID | None = None
    ingestion_run_id: UUID | None = None
    extraction_model_id: str | None = None
    embedding_model_id: str | None = None

    # ---- construction ---------------------------------------------------------------------

    @classmethod
    def for_episode(
        cls,
        episode: Episode,
        *,
        device_id: str,
        extraction_model_id: str | None,
        source: Source | None = None,
        version: SourceVersion | None = None,
        embedding_model_id: str | None = None,
        ingestion_run_id: UUID | None = None,
        project_id: str | None = None,
    ) -> EpisodeProvenance:
        """Assemble from rows the caller already loaded. Nothing is queried here."""
        return cls(
            device_id=device_id,
            observed_at=ensure_utc(episode.observed_at),
            episode_id=episode.id,
            project_id=project_id or episode.project_id or (source.project_id if source else None),
            source_id=episode.source_id or (source.id if source else None),
            source_uri=source.uri if source else None,
            source_hash=version.content_hash if version else None,
            source_version=episode.version_id or (version.id if version else None),
            ingestion_run_id=ingestion_run_id or episode.ingestion_run_id,
            extraction_model_id=extraction_model_id,
            embedding_model_id=embedding_model_id,
        )

    @classmethod
    def from_row(
        cls,
        row: Mapping[str, Any],
        *,
        device_id: str,
        extraction_model_id: str | None,
        embedding_model_id: str | None = None,
    ) -> EpisodeProvenance:
        """Assemble from a joined episode/source/version row (the ingestion claim query)."""
        return cls(
            device_id=device_id,
            observed_at=ensure_utc(row["observed_at"]),
            episode_id=row.get("id") or row.get("episode_id"),
            project_id=row.get("project_id"),
            source_id=row.get("source_id"),
            source_uri=row.get("source_uri") or row.get("uri"),
            source_hash=row.get("source_hash") or row.get("content_hash"),
            source_version=row.get("version_id") or row.get("source_version"),
            ingestion_run_id=row.get("ingestion_run_id"),
            extraction_model_id=extraction_model_id,
            embedding_model_id=embedding_model_id,
        )

    def with_model(self, extraction_model_id: str) -> EpisodeProvenance:
        """ADR-0014: re-stamp for a different extraction model (re-extraction under one model)."""
        return replace(self, extraction_model_id=extraction_model_id)

    def as_deterministic(self) -> EpisodeProvenance:
        """The same episode context, attributed to the deterministic pseudo-model."""
        return replace(self, extraction_model_id=DETERMINISTIC_MODEL_ID)

    # ---- per-row stamps -------------------------------------------------------------------

    def _base(self, **overrides: Any) -> Provenance:
        return build_provenance(
            device_id=self.device_id,
            observed_at=self.observed_at,
            source_id=self.source_id,
            source_uri=self.source_uri,
            source_hash=self.source_hash,
            source_version=self.source_version,
            project_id=self.project_id,
            extraction_model_id=self.extraction_model_id,
            embedding_model_id=self.embedding_model_id,
            ingestion_run_id=self.ingestion_run_id,
            episode_id=self.episode_id,
            **overrides,
        )

    def fact(
        self, *, valid_from: datetime, valid_to: datetime | None = None, confidence: float = 1.0
    ) -> Provenance:
        return self._base(valid_from=valid_from, valid_to=valid_to, confidence=confidence)

    def artifact(
        self, *, valid_from: datetime, valid_to: datetime | None = None, confidence: float = 1.0
    ) -> Provenance:
        return self._base(valid_from=valid_from, valid_to=valid_to, confidence=confidence)

    def mention(
        self,
        *,
        confidence: float = 1.0,
        heading_path: Sequence[str] = (),
        char_start: int | None = None,
        char_end: int | None = None,
        valid_from: datetime | None = None,
    ) -> Provenance:
        return self._base(
            valid_from=valid_from if valid_from is not None else self.observed_at,
            confidence=confidence,
            heading_path=list(heading_path),
            char_start=char_start,
            char_end=char_end,
        )

    def chunk(
        self,
        *,
        heading_path: Sequence[str] = (),
        char_start: int | None = None,
        char_end: int | None = None,
    ) -> Provenance:
        return self._base(
            valid_from=self.observed_at,
            heading_path=list(heading_path),
            char_start=char_start,
            char_end=char_end,
        )
