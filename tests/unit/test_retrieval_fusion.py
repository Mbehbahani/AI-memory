"""P9-T01 (A09): Reciprocal Rank Fusion (``retrieval.md`` §3).

RRF is the one place where "how a hit was found" becomes a number, and the gold-set evaluation
(ADR-0010) can only detect a regression if that number is reproducible. These tests pin the
arithmetic (``1 / (rrf_k + rank)``), the identity key, the ordering and the degraded-mode behaviour
of fusing a single list - no database involved.
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from aimemory.domain.enums import ObjectType, RetrieverKind
from aimemory.domain.retrieval import Candidate
from aimemory.retrieval.fusion import reciprocal_rank_fusion, rrf_contribution

RRF_K = 60


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:fusion:{label}")


def _list(retriever: RetrieverKind, labels: list[str], *, object_type=ObjectType.CHUNK):
    return [
        Candidate(
            object_type=object_type,
            object_id=_id(label),
            retriever=retriever,
            rank=rank,
            raw_score=1.0 / rank,
        )
        for rank, label in enumerate(labels, start=1)
    ]


def test_rrf_contribution_is_one_over_k_plus_rank() -> None:
    assert rrf_contribution(1, 60) == pytest.approx(1 / 61)
    assert rrf_contribution(40, 60) == pytest.approx(1 / 100)


def test_rrf_contribution_rejects_a_zero_based_rank() -> None:
    with pytest.raises(ValueError, match="1-based"):
        rrf_contribution(0, 60)


def test_a_hit_found_by_both_retrievers_sums_both_contributions() -> None:
    semantic = _list(RetrieverKind.SEMANTIC, ["a", "b", "c"])
    keyword = _list(RetrieverKind.KEYWORD, ["c", "a"])

    fused = reciprocal_rank_fusion([semantic, keyword], rrf_k=RRF_K)
    scores = {hit.object_id: hit.rrf_score for hit in fused}

    assert scores[_id("a")] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[_id("c")] == pytest.approx(1 / 63 + 1 / 61)
    assert scores[_id("b")] == pytest.approx(1 / 62)


def test_agreement_between_retrievers_outranks_a_single_top_hit() -> None:
    # 'a' is only rank 3 semantically and rank 2 by keyword, but both found it; 'z' is the semantic
    # top hit and nothing else saw it. RRF (k=60) puts the agreed-on hit first - that is the point.
    semantic = _list(RetrieverKind.SEMANTIC, ["z", "y", "a"])
    keyword = _list(RetrieverKind.KEYWORD, ["w", "a"])

    fused = reciprocal_rank_fusion([semantic, keyword], rrf_k=RRF_K)

    assert fused[0].object_id == _id("a")
    assert [r.value for r in fused[0].retrievers] == ["semantic", "keyword"]


def test_ranks_and_raw_scores_of_every_contributing_retriever_are_recorded() -> None:
    fused = reciprocal_rank_fusion(
        [_list(RetrieverKind.SEMANTIC, ["a"]), _list(RetrieverKind.KEYWORD, ["x", "a"])],
        rrf_k=RRF_K,
    )
    hit = next(h for h in fused if h.object_id == _id("a"))

    assert hit.ranks == {"semantic": 1, "keyword": 2}
    assert hit.raw_scores == {"semantic": pytest.approx(1.0), "keyword": pytest.approx(0.5)}


def test_identity_is_object_type_plus_object_id_so_a_chunk_and_an_artifact_never_merge() -> None:
    shared = _id("shared")
    semantic = [
        Candidate(
            object_type=ObjectType.CHUNK,
            object_id=shared,
            retriever=RetrieverKind.SEMANTIC,
            rank=1,
            raw_score=0.9,
        ),
        Candidate(
            object_type=ObjectType.ARTIFACT,
            object_id=shared,
            retriever=RetrieverKind.SEMANTIC,
            rank=2,
            raw_score=0.8,
        ),
    ]

    fused = reciprocal_rank_fusion([semantic], rrf_k=RRF_K)

    assert len(fused) == 2
    assert {hit.object_type for hit in fused} == {ObjectType.CHUNK, ObjectType.ARTIFACT}


def test_one_retriever_cannot_pay_twice_for_the_same_object() -> None:
    duplicated = _list(RetrieverKind.KEYWORD, ["a", "a"])

    fused = reciprocal_rank_fusion([duplicated], rrf_k=RRF_K)

    assert len(fused) == 1
    assert fused[0].rrf_score == pytest.approx(1 / 61)
    assert fused[0].ranks == {"keyword": 1}


def test_fusing_a_single_list_preserves_its_order_the_keyword_only_degraded_mode() -> None:
    keyword = _list(RetrieverKind.KEYWORD, ["a", "b", "c", "d"])

    fused = reciprocal_rank_fusion([keyword], rrf_k=RRF_K)

    assert [hit.object_id for hit in fused] == [_id(x) for x in ("a", "b", "c", "d")]


def test_empty_and_absent_lists_fuse_to_nothing() -> None:
    assert reciprocal_rank_fusion([], rrf_k=RRF_K) == []
    assert reciprocal_rank_fusion([[], []], rrf_k=RRF_K) == []


def test_limit_applies_fused_top_k() -> None:
    fused = reciprocal_rank_fusion(
        [_list(RetrieverKind.SEMANTIC, [f"c{i}" for i in range(40)])], rrf_k=RRF_K, limit=15
    )

    assert len(fused) == 15


def test_ordering_is_deterministic_for_equal_scores() -> None:
    # Two objects at rank 1 in two different retrievers have identical RRF scores; the tie-break is
    # (object_type, object_id) so two runs - and two machines - agree (retrieval.md deviation 3).
    first = reciprocal_rank_fusion(
        [_list(RetrieverKind.SEMANTIC, ["a"]), _list(RetrieverKind.KEYWORD, ["b"])], rrf_k=RRF_K
    )
    second = reciprocal_rank_fusion(
        [_list(RetrieverKind.KEYWORD, ["b"]), _list(RetrieverKind.SEMANTIC, ["a"])], rrf_k=RRF_K
    )

    assert [h.object_id for h in first] == [h.object_id for h in second]
    assert first[0].rrf_score == pytest.approx(second[0].rrf_score)


def test_rrf_k_must_be_positive() -> None:
    with pytest.raises(ValueError, match="rrf_k"):
        reciprocal_rank_fusion([_list(RetrieverKind.SEMANTIC, ["a"])], rrf_k=0)
