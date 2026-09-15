"""Stage 6: boosts and final ranking (``retrieval.md`` §6).

    score = rrf_score
          + project_match     if hit.project_id in query.project_ids          # +0.10
          + entity_linked     if the hit shares an entity with the expansion  # +0.10
          + recency_boost(hit)                                               # <= +0.10
          + unconfirmed_penalty if hit.status == 'unconfirmed'               # -0.15
          + low_trust          if the source's trust band is 'low'           # -0.15 (AC-6)

    recency_boost(hit) = 0.10 * 0.5 ** (age_days(observed_at) / recency_half_life_days)

Every term is written into ``boosts`` separately and never folded into the total, because a ranking
change has to be explainable from a stored ``retrieval_logs`` row without re-running the query.
Terms that evaluate to zero are omitted, so the map reads as "what actually moved this hit".

Ordering is ``score`` desc, then ``observed_at`` desc, then ``object_id`` - deterministic by
construction (``retrieval.md`` deviation 3), so a gold-set difference is a regression and not noise.

The ``entity_linked`` term is computed here but its input (which hits share an entity with the
1-hop graph expansion) arrives in P10; until then callers pass an empty set and the term is absent,
which is an honest "no expansion ran", not a silent zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from ..common.time import ensure_utc, utc_now
from ..domain.enums import FactStatus, Trust
from ..domain.retrieval import RetrievalConfig
from .types import HitKey, HitMetadata, RankedCandidate, ScoredCandidate

__all__ = [
    "MAX_RECENCY_BOOST",
    "apply_boosts",
    "compute_boosts",
    "rank_candidates",
    "recency_boost",
]

#: The recency term is capped at the magnitude of the other positive boosts so no single, very
#: recent but weakly matching chunk can outrank a strong match (``retrieval.md`` deviation 1).
MAX_RECENCY_BOOST = 0.10


def recency_boost(
    observed_at: datetime | None,
    *,
    half_life_days: float,
    now: datetime | None = None,
    maximum: float = MAX_RECENCY_BOOST,
) -> float:
    """``maximum * 0.5 ** (age_days / half_life_days)``, clamped to ``[0, maximum]``.

    ``None`` (an object with no observation time) scores 0.0 rather than "infinitely old" - it is
    unknown, not stale. A future ``observed_at`` (clock skew, a stated future date) is clamped to
    ``maximum`` instead of growing without bound.
    """
    if observed_at is None:
        return 0.0
    if half_life_days <= 0:
        raise ValueError("boosts.recency_half_life_days must be > 0")
    reference = ensure_utc(now) if now is not None else utc_now()
    age_days = (reference - ensure_utc(observed_at)).total_seconds() / 86400.0
    if age_days <= 0:
        return maximum
    return maximum * (0.5 ** (age_days / half_life_days))


def compute_boosts(
    metadata: HitMetadata,
    *,
    config: RetrievalConfig,
    project_ids: Sequence[str] = (),
    entity_linked: bool = False,
    now: datetime | None = None,
) -> dict[str, float]:
    """The per-hit boost map. Keys are stable identifiers used by the Ops page and the gold set.

    Only non-zero terms appear. ``project_match`` requires an explicitly scoped query: an unscoped
    search has nothing to match against and every hit would receive it, which is not a ranking
    signal at all.
    """
    boosts: dict[str, float] = {}

    scoped_match = bool(project_ids) and metadata.project_id in set(project_ids)
    if scoped_match and config.boost_project_match:
        boosts["project_match"] = config.boost_project_match

    if entity_linked and config.boost_entity_linked:
        boosts["entity_linked"] = config.boost_entity_linked

    recency = recency_boost(
        metadata.observed_at, half_life_days=config.recency_half_life_days, now=now
    )
    if recency:
        boosts["recency"] = recency

    if metadata.status == FactStatus.UNCONFIRMED.value and config.unconfirmed_penalty:
        boosts["unconfirmed"] = config.unconfirmed_penalty

    if metadata.trust == Trust.LOW.value and config.low_trust_penalty:
        boosts["low_trust"] = config.low_trust_penalty

    return boosts


def apply_boosts(
    fused: ScoredCandidate,
    metadata: HitMetadata,
    *,
    config: RetrievalConfig,
    project_ids: Sequence[str] = (),
    entity_linked: bool = False,
    now: datetime | None = None,
) -> RankedCandidate:
    """One fused candidate + its metadata -> a scored, still unranked :class:`RankedCandidate`."""
    boosts = compute_boosts(
        metadata,
        config=config,
        project_ids=project_ids,
        entity_linked=entity_linked,
        now=now,
    )
    return RankedCandidate(
        object_type=fused.object_type,
        object_id=fused.object_id,
        rrf_score=fused.rrf_score,
        score=fused.rrf_score + sum(boosts.values()),
        boosts=boosts,
        retrievers=list(fused.retrievers),
        ranks=dict(fused.ranks),
        rank=0,
        metadata=metadata,
    )


#: Sorted ascending, so ``-score`` and ``-timestamp`` give "best first, newest first".
_EPOCH_SECONDS = 0.0


def _tie_break(hit: RankedCandidate) -> tuple[float, float, str]:
    observed = hit.metadata.observed_at
    seconds = ensure_utc(observed).timestamp() if observed is not None else _EPOCH_SECONDS
    return (-hit.score, -seconds, str(hit.object_id))


def rank_candidates(
    fused: Iterable[ScoredCandidate],
    metadata: Mapping[str, HitMetadata],
    *,
    config: RetrievalConfig,
    project_ids: Sequence[str] = (),
    entity_linked_keys: Iterable[HitKey] = (),
    limit: int | None = None,
    now: datetime | None = None,
) -> list[RankedCandidate]:
    """Boost, sort and number the fused candidates; keep ``limit`` (``candidates.final_k``).

    A fused candidate with no metadata entry is dropped: without it the hit cannot be ranked,
    cited or traced, and ``retrieval.md`` §7 forbids returning something the system cannot trace.
    Callers surface that as a warning (see :mod:`aimemory.retrieval.pipeline`).
    """
    linked = set(entity_linked_keys)
    ranked: list[RankedCandidate] = []
    for candidate in fused:
        key = f"{candidate.object_type.value}:{candidate.object_id}"
        meta = metadata.get(key)
        if meta is None:
            continue
        ranked.append(
            apply_boosts(
                candidate,
                meta,
                config=config,
                project_ids=project_ids,
                entity_linked=(candidate.object_type, candidate.object_id) in linked,
                now=now,
            )
        )
    ranked.sort(key=_tie_break)
    if limit is not None:
        ranked = ranked[:limit]
    for position, hit in enumerate(ranked, start=1):
        hit.rank = position
    return ranked
