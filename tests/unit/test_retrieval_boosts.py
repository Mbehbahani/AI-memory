"""P9-T01 (A09): boosts and final ranking (``retrieval.md`` §6, revised after the P14 evaluation).

Three properties are load-bearing and are tested here rather than inferred:

1. **Every boost is recorded separately.** ``ScoredHit.boosts`` is what makes a ranking change
   explainable from a stored ``retrieval_logs`` row; a term folded into the total is a term nobody
   can audit. Since the P14 fix the map also carries the base components, so
   ``score == sum(boosts.values())`` exactly.
2. **Boosts are proportional, not absolute.** A configured ``0.10`` means "+10 % of this hit's
   relevance". The gold-set evaluation measured what absolute points did instead: a penalty of
   -0.15 against an RRF range of 0.003 removed the *correct* document from the results entirely
   (the Apache Tika clipping, rank-1 semantic candidate, returned last at score -0.034). The tests
   below pin the proportionality so that cannot come back.
3. **Ordering is deterministic** (score desc, ``rrf_score`` desc, ``observed_at`` desc,
   ``object_id``). Without it the gold-set numbers wobble and a regression cannot be told apart
   from noise (ADR-0010).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from aimemory.domain.enums import ObjectType, RetrieverKind
from aimemory.domain.retrieval import RetrievalConfig
from aimemory.retrieval.boosts import (
    MAX_RECENCY_BOOST,
    apply_boosts,
    compute_boosts,
    rank_candidates,
    recency_boost,
)
from aimemory.retrieval.relevance import RelevanceScore
from aimemory.retrieval.types import HitMetadata, ScoredCandidate

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)

CONFIG = RetrievalConfig(
    boost_project_match=0.10,
    boost_entity_linked=0.10,
    unconfirmed_penalty=-0.15,
    low_trust_penalty=-0.15,
    recency_half_life_days=180.0,
    final_k=10,
)


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:boosts:{label}")


def _meta(label: str, **overrides) -> HitMetadata:
    payload = {
        "object_type": ObjectType.CHUNK,
        "object_id": _id(label),
        "project_id": "joblab-de",
        "text": f"chunk {label}",
        "observed_at": NOW,
        "trust": "high",
    }
    payload.update(overrides)
    return HitMetadata(**payload)


def _fused(label: str, score: float, **overrides) -> ScoredCandidate:
    return ScoredCandidate(
        object_type=overrides.get("object_type", ObjectType.CHUNK),
        object_id=_id(label),
        rrf_score=score,
        retrievers=[RetrieverKind.SEMANTIC],
        ranks={"semantic": 1},
    )


# ------------------------------------------------------------------------------- recency decay


def test_recency_boost_is_the_maximum_at_zero_age() -> None:
    assert recency_boost(NOW, half_life_days=180, now=NOW) == pytest.approx(MAX_RECENCY_BOOST)


def test_recency_boost_halves_every_half_life() -> None:
    one = recency_boost(NOW - timedelta(days=180), half_life_days=180, now=NOW)
    two = recency_boost(NOW - timedelta(days=360), half_life_days=180, now=NOW)

    assert one == pytest.approx(MAX_RECENCY_BOOST / 2)
    assert two == pytest.approx(MAX_RECENCY_BOOST / 4)


def test_recency_boost_of_an_unknown_observation_time_is_zero_not_infinitely_old() -> None:
    assert recency_boost(None, half_life_days=180, now=NOW) == 0.0


def test_a_future_observation_time_is_clamped_to_the_maximum() -> None:
    future = recency_boost(NOW + timedelta(days=3650), half_life_days=180, now=NOW)

    assert future == pytest.approx(MAX_RECENCY_BOOST)


def test_recency_boost_never_exceeds_the_positive_boost_magnitude() -> None:
    for days in (0, 1, 30, 180, 3650):
        value = recency_boost(NOW - timedelta(days=days), half_life_days=180, now=NOW)
        assert 0.0 <= value <= MAX_RECENCY_BOOST


def test_a_non_positive_half_life_is_rejected() -> None:
    with pytest.raises(ValueError, match="half_life"):
        recency_boost(NOW, half_life_days=0, now=NOW)


# -------------------------------------------------------------------------------- boost terms


def test_project_match_is_applied_only_for_an_explicitly_scoped_query() -> None:
    meta = _meta("a", observed_at=None)

    scoped = compute_boosts(meta, config=CONFIG, project_ids=["joblab-de"], now=NOW)
    unscoped = compute_boosts(meta, config=CONFIG, project_ids=[], now=NOW)
    other = compute_boosts(meta, config=CONFIG, project_ids=["other-project"], now=NOW)

    assert scoped == {"project_match": pytest.approx(0.10)}
    assert unscoped == {}
    assert other == {}


def test_entity_linked_is_absent_until_graph_expansion_supplies_it() -> None:
    meta = _meta("a", observed_at=None, project_id=None)

    assert "entity_linked" not in compute_boosts(meta, config=CONFIG, now=NOW)
    assert compute_boosts(meta, config=CONFIG, entity_linked=True, now=NOW) == {
        "entity_linked": pytest.approx(0.10)
    }


def test_unconfirmed_is_penalised_and_kept_not_dropped() -> None:
    meta = _meta("a", observed_at=None, project_id=None, status="unconfirmed")

    boosts = compute_boosts(meta, config=CONFIG, now=NOW)

    assert boosts == {"unconfirmed": pytest.approx(-0.15)}


def test_low_trust_sources_carry_their_own_named_penalty() -> None:
    meta = _meta("a", observed_at=None, project_id=None, trust="low")

    boosts = compute_boosts(meta, config=CONFIG, now=NOW)

    assert boosts == {"low_trust": pytest.approx(-0.15)}
    assert "unconfirmed" not in boosts, "AC-6 clippings are reported separately from ADR-0005 rule 3"


def test_every_term_is_recorded_separately_and_sums_to_the_score() -> None:
    meta = _meta("a", status="unconfirmed", trust="low")
    relevance = RelevanceScore(relevance=0.60, parts={"semantic": 0.35, "lexical": 0.25})

    hit = apply_boosts(
        _fused("a", 0.5),
        meta,
        config=CONFIG,
        relevance=relevance,
        project_ids=["joblab-de"],
        entity_linked=True,
        now=NOW,
    )

    assert set(hit.boosts) == {
        "semantic",
        "lexical",
        "project_match",
        "entity_linked",
        "recency",
        "unconfirmed",
        "low_trust",
    }
    # The map is a complete, auditable decomposition of the score - base components included.
    assert hit.score == pytest.approx(sum(hit.boosts.values()))
    assert hit.relevance == pytest.approx(0.60)
    assert hit.boost_fractions["low_trust"] == pytest.approx(-0.15)
    assert hit.boosts["low_trust"] == pytest.approx(-0.15 * 0.60)


def test_boosts_are_a_fraction_of_relevance_so_they_nudge_instead_of_deciding() -> None:
    """The P14 regression in one assertion: a penalised strong hit still beats a clean weak one."""
    strong = apply_boosts(
        _fused("strong", 0.016),
        _meta("strong", project_id=None, observed_at=None, trust="low"),
        config=CONFIG,
        relevance=RelevanceScore(relevance=0.65, parts={"semantic": 0.65}),
        now=NOW,
    )
    weak = apply_boosts(
        _fused("weak", 0.015),
        _meta("weak", project_id=None, observed_at=None),
        config=CONFIG,
        relevance=RelevanceScore(relevance=0.20, parts={"semantic": 0.20}),
        now=NOW,
    )

    assert strong.score == pytest.approx(0.65 * 0.85)
    assert weak.score == pytest.approx(0.20)
    assert strong.score > weak.score, "AC-6 asks clippings to rank lower, not to be unreachable"


def test_without_a_relevance_score_the_rrf_value_is_the_base() -> None:
    """Callers with no database session (unit tests, total degradation) still get an ordering."""
    hit = apply_boosts(
        _fused("a", 0.25), _meta("a", project_id=None, observed_at=None), config=CONFIG, now=NOW
    )

    assert hit.relevance == pytest.approx(0.25)
    assert hit.boosts == {"rrf": pytest.approx(0.25)}
    assert hit.score == pytest.approx(0.25)


def test_zero_valued_terms_are_omitted_so_the_map_reads_as_what_moved_the_hit() -> None:
    meta = _meta("a", project_id=None, observed_at=None)

    assert compute_boosts(meta, config=CONFIG, now=NOW) == {}


# ------------------------------------------------------------------------------------ ranking


def test_ranking_sorts_by_score_and_numbers_from_one() -> None:
    fused = [_fused("low", 0.01), _fused("high", 0.90), _fused("mid", 0.50)]
    metadata = {f"chunk:{_id(x)}": _meta(x, observed_at=None, project_id=None) for x in ("low", "high", "mid")}

    ranked = rank_candidates(fused, metadata, config=CONFIG, now=NOW)

    assert [hit.object_id for hit in ranked] == [_id("high"), _id("mid"), _id("low")]
    assert [hit.rank for hit in ranked] == [1, 2, 3]


def test_equal_scores_break_on_observed_at_descending() -> None:
    # Both observation times are in the future, so both recency terms clamp to the same maximum:
    # the scores are exactly equal and only the tie-break can order them.
    fused = [_fused("x", 0.20), _fused("y", 0.20)]
    metadata = {
        f"chunk:{_id('x')}": _meta("x", observed_at=NOW + timedelta(days=1), project_id=None),
        f"chunk:{_id('y')}": _meta("y", observed_at=NOW + timedelta(days=10), project_id=None),
    }

    ranked = rank_candidates(fused, metadata, config=CONFIG, now=NOW)

    assert ranked[0].score == pytest.approx(ranked[1].score)
    assert ranked[0].object_id == _id("y")


def test_fully_tied_hits_order_by_object_id_regardless_of_input_order() -> None:
    fused = [_fused("x", 0.20), _fused("y", 0.20)]
    metadata = {
        f"chunk:{_id('x')}": _meta("x", observed_at=NOW, project_id=None),
        f"chunk:{_id('y')}": _meta("y", observed_at=NOW, project_id=None),
    }

    forward = rank_candidates(fused, metadata, config=CONFIG, now=NOW)
    backward = rank_candidates(list(reversed(fused)), metadata, config=CONFIG, now=NOW)

    assert [h.object_id for h in forward] == [h.object_id for h in backward]
    assert [str(h.object_id) for h in forward] == sorted(str(h.object_id) for h in forward)


def test_a_candidate_without_metadata_is_dropped_never_returned_untraceable() -> None:
    fused = [_fused("present", 0.9), _fused("orphan", 0.8)]
    metadata = {f"chunk:{_id('present')}": _meta("present", observed_at=None, project_id=None)}

    ranked = rank_candidates(fused, metadata, config=CONFIG, now=NOW)

    assert [hit.object_id for hit in ranked] == [_id("present")]


def test_limit_applies_final_k_after_boosting_not_before() -> None:
    # The lowest RRF score carries the project boost; with final_k=2 it must still make the cut,
    # which is only true if the limit is applied after boosting.
    fused = [_fused("a", 0.30), _fused("b", 0.20), _fused("c", 0.19)]
    metadata = {
        f"chunk:{_id('a')}": _meta("a", project_id=None, observed_at=None),
        f"chunk:{_id('b')}": _meta("b", project_id=None, observed_at=None),
        f"chunk:{_id('c')}": _meta("c", project_id="joblab-de", observed_at=None),
    }

    ranked = rank_candidates(
        fused, metadata, config=CONFIG, project_ids=["joblab-de"], limit=2, now=NOW
    )

    assert [hit.object_id for hit in ranked] == [_id("a"), _id("c")]
    assert ranked[1].boost_fractions == {"project_match": pytest.approx(0.10)}
    assert ranked[1].boosts["project_match"] == pytest.approx(0.10 * ranked[1].relevance)


def test_entity_linked_keys_reach_only_the_hits_they_name() -> None:
    fused = [_fused("a", 0.30), _fused("b", 0.30)]
    metadata = {
        f"chunk:{_id('a')}": _meta("a", project_id=None, observed_at=None),
        f"chunk:{_id('b')}": _meta("b", project_id=None, observed_at=None),
    }

    ranked = rank_candidates(
        fused,
        metadata,
        config=CONFIG,
        entity_linked_keys={(ObjectType.CHUNK, _id("b"))},
        now=NOW,
    )

    assert ranked[0].object_id == _id("b")
    assert ranked[0].boost_fractions == {"entity_linked": pytest.approx(0.10)}
    assert ranked[1].boost_fractions == {}
