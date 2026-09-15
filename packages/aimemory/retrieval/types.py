"""Internal carriers between the retrieval stages of ``docs/architecture/retrieval.md``.

Why these exist instead of going straight to :class:`~aimemory.domain.retrieval.ScoredHit`:
``ScoredHit`` requires a fully resolved :class:`~aimemory.domain.provenance.Provenance` (the plan's
"provenance completeness 100 %" acceptance criterion), and provenance attachment is stage 7 - P10.
P9 therefore fuses and ranks *candidates*, carrying every field a ``ScoredHit`` will need, and P10
turns a :class:`RankedCandidate` into a ``ScoredHit`` in one place
(:meth:`RankedCandidate.to_scored_hit`). No shape here contradicts the frozen contract; they are the
pre-provenance stage of it.

``HitKey`` - ``(object_type, object_id)`` - is the identity used by Reciprocal Rank Fusion, by the
boost stage and by the deduplication of the two retrievers' candidate lists.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from ..domain.base import DomainModel
from ..domain.enums import ObjectType, RetrieverKind
from ..domain.provenance import Provenance
from ..domain.retrieval import Candidate, ScoredHit

__all__ = [
    "HitKey",
    "HitMetadata",
    "RankedCandidate",
    "RetrieverOutput",
    "ScoredCandidate",
    "hit_key",
]

#: Identity of a retrievable object across both retrievers.
HitKey = tuple[ObjectType, UUID]


def hit_key(candidate: Candidate | HitMetadata | ScoredCandidate | RankedCandidate) -> HitKey:
    """``(object_type, object_id)`` for anything that carries those two fields."""
    return (candidate.object_type, candidate.object_id)


class HitMetadata(DomainModel):
    """Everything about a candidate that ranking, citation and provenance need, read once.

    Both retrievers return this for every candidate they produce, so the boost stage needs no second
    round-trip and the values are guaranteed to be the ones the filters were evaluated against.

    ``observed_at`` is the *source* time (ADR-0005 §6): ``source_versions.observed_at`` for a chunk,
    ``knowledge_artifacts.observed_at`` for an artifact. It drives the recency boost and the
    deterministic tie-break, and it is the axis ``since`` filters on (``temporal.md`` §8).
    """

    object_type: ObjectType
    object_id: UUID
    project_id: str | None = None
    source_id: UUID | None = None
    source_uri: str | None = None
    version_id: UUID | None = None
    title: str | None = None
    text: str = ""
    text_hash: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    observed_at: datetime | None = None
    status: str | None = Field(
        default=None, description="knowledge_artifacts.current_status; None for chunks"
    )
    source_status: str = "active"
    trust: str = "high"
    policy: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    entity_ids: list[UUID] = Field(default_factory=list)


class RetrieverOutput(DomainModel):
    """One retriever's contribution: its ranked candidates plus the metadata rows behind them.

    ``metadata`` is keyed by :data:`HitKey`; the two retrievers' maps are merged before ranking and
    agree by construction (they read the same columns of the same rows).
    """

    retriever: RetrieverKind
    candidates: list[Candidate] = Field(default_factory=list)
    metadata: dict[str, HitMetadata] = Field(
        default_factory=dict, description="Keyed by f'{object_type}:{object_id}' (JSON-safe)"
    )
    degraded: bool = Field(
        default=False, description="True when this retriever could not run (see warnings)"
    )
    warning: str | None = None

    def meta_for(self, key: HitKey) -> HitMetadata | None:
        return self.metadata.get(f"{key[0].value}:{key[1]}")


class ScoredCandidate(DomainModel):
    """Output of Reciprocal Rank Fusion (stage 3) - no boosts applied yet.

    ``ranks`` records the 1-based rank this object had in each contributing retriever's list, which
    is what makes an RRF score reproducible from the logs alone.
    """

    object_type: ObjectType
    object_id: UUID
    rrf_score: float = 0.0
    retrievers: list[RetrieverKind] = Field(default_factory=list)
    ranks: dict[str, int] = Field(default_factory=dict)
    raw_scores: dict[str, float] = Field(default_factory=dict)


class RankedCandidate(DomainModel):
    """Output of stage 6 (boosts + final ranking): an ordered, explainable hit without provenance.

    ``score = rrf_score + sum(boosts.values())`` and every contribution is kept separately, so a
    ranking change is explainable without re-running the query (``retrieval.md`` §6).
    """

    object_type: ObjectType
    object_id: UUID
    rrf_score: float = 0.0
    score: float = 0.0
    boosts: dict[str, float] = Field(default_factory=dict)
    retrievers: list[RetrieverKind] = Field(default_factory=list)
    ranks: dict[str, int] = Field(default_factory=dict)
    rank: int = Field(default=0, ge=0)
    metadata: HitMetadata

    def to_scored_hit(self, provenance: Provenance, *, citation: str = "") -> ScoredHit:
        """Promote to the frozen contract once P10 has resolved provenance and the citation."""
        return ScoredHit(
            object_type=self.object_type,
            object_id=self.object_id,
            title=self.metadata.title,
            text=self.metadata.text,
            score=self.score,
            rrf_score=self.rrf_score,
            boosts=dict(self.boosts),
            retrievers=list(self.retrievers),
            rank=self.rank,
            project_id=self.metadata.project_id,
            entity_ids=list(self.metadata.entity_ids),
            valid_from=self.metadata.valid_from,
            valid_to=self.metadata.valid_to,
            status=self.metadata.status,
            provenance=provenance,
            citation=citation,
        )
