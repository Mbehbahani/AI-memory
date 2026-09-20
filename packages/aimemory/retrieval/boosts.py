"""Stage 6: boosts and final ranking (``retrieval.md`` §6, revised after the P14 evaluation).

    relevance = W_SEMANTIC * cosine + W_LEXICAL * idf_coverage   # aimemory.retrieval.relevance
    score     = relevance * (1 + sum(fractions))                 # == sum(boosts.values())

    fractions = project_match       if hit.project_id in query.project_ids      # +10 %
              + entity_linked       if the hit shares an entity with expansion  # +10 %
              + recency(hit)                                                    # <= +10 %
              + unconfirmed_penalty if hit.status == 'unconfirmed'              # -15 %
              + low_trust           if the source's trust band is 'low'         # -15 % (AC-6)

    recency(hit) = 0.10 * 0.5 ** (age_days(observed_at) / recency_half_life_days)

**Why the weights became proportional instead of absolute points.** The plan's formula
(``score = rrf_score + sum(boosts)``) assumed both sides shared a scale. They did not: MEASURED on
the live corpus (2026-09-18) an RRF score spans ``0.0164 … 0.0133`` over a 40-candidate list - a
total range of 0.003 - while the configured boosts are ``0.10`` to ``0.15``, i.e. 30-50x the entire
relevance signal they were meant to nudge. The ranking was therefore a sort by *boost group* with
relevance only breaking ties inside a group: for the Apache Tika question the rank-1 semantic
candidate (cosine 0.654, the exact expected source) came back **last, at score -0.034**, purely
because its source is a clipping (``low_trust``); and weakly-matching but entity-linked chunks
displaced exactly-matching ones. Reading the same configured numbers as *fractions of the hit's own
relevance* preserves every documented intent ("clippings rank lower", "entity-linked ranks higher")
and makes a boost a nudge again: -15 % of a strong hit still beats a weak one.

``config/retrieval.yaml`` is unchanged; ``0.10`` now reads "+10 %" rather than "+0.10 points".

Every term is written into ``boosts`` separately and never folded into the total, because a ranking
change has to be explainable from a stored ``retrieval_logs`` row without re-running the query. The
map also carries the base components (``semantic``, ``lexical``), so ``score == sum(boosts.values())``
holds exactly and the whole score is explainable from that one dict; the unscaled percentages stay
available in :attr:`RankedCandidate.boost_fractions`. Terms that evaluate to zero are omitted, so
the map reads as "what actually moved this hit".

Ordering is ``score`` desc, then ``rrf_score`` desc (rank agreement breaks a scoring tie), then
``observed_at`` desc, then ``object_id`` - deterministic by construction (``retrieval.md``
deviation 3), so a gold-set difference is a regression and not noise.

The ``entity_linked`` term's input (which hits share an entity with the 1-hop graph expansion)
arrives from P10; callers that ran no expansion pass an empty set and the term is absent, which is
an honest "no expansion ran", not a silent zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from ..common.time import ensure_utc, utc_now
from ..domain.enums import FactStatus, Trust
from ..domain.retrieval import RetrievalConfig
from .relevance import RelevanceScore
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
    """The per-hit boost map as **fractions** of the hit's relevance (``0.10`` = "+10 %").

    Keys are stable identifiers used by the Ops page and the gold set.

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
    relevance: RelevanceScore | None = None,
    project_ids: Sequence[str] = (),
    entity_linked: bool = False,
    now: datetime | None = None,
) -> RankedCandidate:
    """One fused candidate + its metadata -> a scored, still unranked :class:`RankedCandidate`.

    ``relevance`` is the calibrated [0, 1] base from :mod:`aimemory.retrieval.relevance`. When it is
    ``None`` - a caller with no database session, or the path where *both* evidence channels failed -
    the RRF score is the base, which degrades to the pre-P14 ordering instead of scoring everything 0.
    """
    fractions = compute_boosts(
        metadata,
        config=config,
        project_ids=project_ids,
        entity_linked=entity_linked,
        now=now,
    )
    base = relevance.relevance if relevance is not None else fused.rrf_score
    parts = dict(relevance.parts) if relevance is not None else {"rrf": fused.rrf_score}
    boosts = {name: value for name, value in parts.items() if value}
    for name, fraction in fractions.items():
        points = fraction * base
        if points:
            boosts[name] = points
    return RankedCandidate(
        object_type=fused.object_type,
        object_id=fused.object_id,
        rrf_score=fused.rrf_score,
        relevance=base,
        score=base * (1.0 + sum(fractions.values())),
        boosts=boosts,
        boost_fractions=fractions,
        retrievers=list(fused.retrievers),
        ranks=dict(fused.ranks),
        rank=0,
        metadata=metadata,
    )


#: Sorted ascending, so ``-score`` and ``-timestamp`` give "best first, newest first".
_EPOCH_SECONDS = 0.0


def _tie_break(hit: RankedCandidate) -> tuple[float, float, float, str]:
    observed = hit.metadata.observed_at
    seconds = ensure_utc(observed).timestamp() if observed is not None else _EPOCH_SECONDS
    return (-hit.score, -hit.rrf_score, -seconds, str(hit.object_id))


def rank_candidates(
    fused: Iterable[ScoredCandidate],
    metadata: Mapping[str, HitMetadata],
    *,
    config: RetrievalConfig,
    relevance: Mapping[HitKey, RelevanceScore] | None = None,
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
        hit_key: HitKey = (candidate.object_type, candidate.object_id)
        ranked.append(
            apply_boosts(
                candidate,
                meta,
                config=config,
                relevance=(relevance or {}).get(hit_key),
                project_ids=project_ids,
                entity_linked=hit_key in linked,
                now=now,
            )
        )
    ranked.sort(key=_tie_break)
    if limit is not None:
        ranked = ranked[:limit]
    for position, hit in enumerate(ranked, start=1):
        hit.rank = position
    return ranked
