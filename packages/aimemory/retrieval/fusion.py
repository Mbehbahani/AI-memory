"""Stage 3: Reciprocal Rank Fusion of the retriever candidate lists (``retrieval.md`` §3).

    FOR each retriever r IN (semantic, keyword):
        FOR each candidate c at 1-based rank i in r:
            rrf[key(c)] += 1 / (rrf_k + i)

RRF is used because a cosine similarity and a ``ts_rank_cd`` value are not comparable: rank fusion
needs no score normalization, no calibration set and no training data. ``rrf_k`` (60) damps the
head of each list so a single retriever cannot dominate on its own top hit alone.

The function is pure - it takes ranked candidate lists and returns fused candidates - which is what
lets ``tests/unit/test_retrieval_fusion.py`` pin the arithmetic without a database.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ..domain.retrieval import Candidate
from .types import HitKey, HitMetadata, RetrieverOutput, ScoredCandidate

__all__ = ["merge_metadata", "reciprocal_rank_fusion", "rrf_contribution"]


def rrf_contribution(rank: int, rrf_k: int) -> float:
    """One retriever's contribution for a candidate at 1-based ``rank``: ``1 / (rrf_k + rank)``."""
    if rank < 1:
        raise ValueError("RRF ranks are 1-based")
    return 1.0 / (rrf_k + rank)


def reciprocal_rank_fusion(
    candidate_lists: Iterable[Sequence[Candidate]],
    *,
    rrf_k: int = 60,
    limit: int | None = None,
) -> list[ScoredCandidate]:
    """Fuse ranked candidate lists into one ordered list of :class:`ScoredCandidate`.

    ``limit`` is ``candidates.fused_top_k`` (15). Ordering is ``rrf_score`` descending, then
    ``object_type``/``object_id`` ascending, so the result is byte-for-byte reproducible across runs
    - the precondition for comparing two gold-set reports (ADR-0010).

    A candidate list is allowed to be empty (a degraded retriever); fusing one list is the identity
    ranking, which is precisely the "embedding down -> keyword only" behaviour the plan requires.
    """
    if rrf_k < 1:
        raise ValueError("fusion.rrf_k must be >= 1")

    fused: dict[HitKey, ScoredCandidate] = {}
    for candidates in candidate_lists:
        seen: set[HitKey] = set()
        for candidate in candidates:
            key: HitKey = (candidate.object_type, candidate.object_id)
            if key in seen:
                # One retriever must not pay twice for the same object.
                continue
            seen.add(key)
            entry = fused.get(key)
            if entry is None:
                entry = ScoredCandidate(object_type=key[0], object_id=key[1])
                fused[key] = entry
            entry.rrf_score += rrf_contribution(candidate.rank, rrf_k)
            if candidate.retriever not in entry.retrievers:
                entry.retrievers = [*entry.retrievers, candidate.retriever]
            entry.ranks = {**entry.ranks, candidate.retriever.value: candidate.rank}
            entry.raw_scores = {**entry.raw_scores, candidate.retriever.value: candidate.raw_score}

    ordered = sorted(
        fused.values(),
        key=lambda hit: (-hit.rrf_score, hit.object_type.value, str(hit.object_id)),
    )
    return ordered if limit is None else ordered[:limit]


def merge_metadata(outputs: Sequence[RetrieverOutput]) -> dict[str, HitMetadata]:
    """Merge the retrievers' metadata maps. Later outputs never overwrite an earlier entry: both
    retrievers read the same columns of the same rows, so the first one wins by definition."""
    merged: dict[str, HitMetadata] = {}
    for output in outputs:
        for key, meta in output.metadata.items():
            merged.setdefault(key, meta)
    return merged
