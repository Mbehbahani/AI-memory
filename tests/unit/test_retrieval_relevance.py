"""P9 (A09): the calibrated relevance base score (``aimemory.retrieval.relevance``).

This module exists because the P14 gold-set evaluation MEASURED two defects with one root cause:
Reciprocal Rank Fusion is rank-based, so the top hit of *every* query scored ``1 / (rrf_k + 1)``
regardless of how well it matched, and the configured boosts (±0.10 … ±0.15) were 30-50x the whole
range of that signal. The tests below pin the properties that fix depends on:

1. relevance is in [0, 1] and is built from evidence with an absolute meaning (cosine similarity and
   IDF-weighted term coverage), so an unanswerable question can score lower than an answerable one;
2. a candidate that one retriever never returned is imputed that retriever's *minimum* observed
   score - an upper bound, since it ranked below the cut-off - instead of 0.0;
3. a missing channel renormalises the weights (keyword-only degradation still yields a [0, 1]
   score), and the total absence of both channels falls back to normalised RRF rather than zero.

The SQL side (``analyse_query``/``lexical_coverage``) is exercised against a real corpus in
``tests/integration/test_retrieval_candidates.py``; here the arithmetic is pinned without a database.
"""

from __future__ import annotations

import uuid

import pytest
from aimemory.domain.enums import ObjectType
from aimemory.retrieval.relevance import (
    W_LEXICAL,
    W_SEMANTIC,
    QueryLexicon,
    score_relevance,
)
from aimemory.retrieval.types import HitKey


def _key(label: str) -> HitKey:
    return (ObjectType.CHUNK, uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:relevance:{label}"))


A, B, C = _key("a"), _key("b"), _key("c")


def test_weights_sum_to_one_so_relevance_stays_in_the_unit_interval() -> None:
    assert W_SEMANTIC + W_LEXICAL == pytest.approx(1.0)

    scored = score_relevance([A], semantic_scores={A: 1.0}, lexical_scores={A: 1.0})

    assert scored[A].relevance == pytest.approx(1.0)


def test_relevance_blends_the_two_channels_and_reports_both_parts() -> None:
    scored = score_relevance([A], semantic_scores={A: 0.80}, lexical_scores={A: 0.40})

    assert scored[A].relevance == pytest.approx(0.5 * 0.80 + 0.5 * 0.40)
    assert scored[A].parts == {
        "semantic": pytest.approx(0.5 * 0.80),
        "lexical": pytest.approx(0.5 * 0.40),
    }
    assert sum(scored[A].parts.values()) == pytest.approx(scored[A].relevance)


def test_a_specific_hit_outscores_a_generic_one_with_the_same_embedding_similarity() -> None:
    """A12's finding 1 in one assertion: term rarity is what MiniLM cannot see."""
    scored = score_relevance(
        [A, B], semantic_scores={A: 0.55, B: 0.55}, lexical_scores={A: 0.90, B: 0.10}
    )

    assert scored[A].relevance > scored[B].relevance


def test_a_candidate_missing_from_the_semantic_list_is_imputed_the_list_minimum() -> None:
    # C was found by the keyword retriever only: its true cosine is <= the semantic list's lowest
    # (0.30), so 0.30 is an upper bound and 0.0 would be a lie that buries a legitimate hit.
    scored = score_relevance(
        [A, B, C], semantic_scores={A: 0.70, B: 0.30}, lexical_scores={A: 0.0, B: 0.0, C: 0.0}
    )

    assert scored[C].parts["semantic"] == pytest.approx(W_SEMANTIC * 0.30)


def test_similarities_outside_the_unit_interval_are_clamped() -> None:
    scored = score_relevance([A, B], semantic_scores={A: -0.20, B: 1.40}, lexical_scores=None)

    assert scored[A].relevance == pytest.approx(0.0)
    assert scored[B].relevance == pytest.approx(1.0)


def test_keyword_only_degradation_renormalises_to_the_lexical_channel() -> None:
    scored = score_relevance([A], semantic_scores={}, lexical_scores={A: 0.60})

    assert scored[A].relevance == pytest.approx(0.60), "a missing channel must not halve the score"
    assert set(scored[A].parts) == {"lexical"}


def test_a_stopword_only_query_renormalises_to_the_semantic_channel() -> None:
    scored = score_relevance([A], semantic_scores={A: 0.42}, lexical_scores=None)

    assert scored[A].relevance == pytest.approx(0.42)
    assert set(scored[A].parts) == {"semantic"}


def test_with_no_evidence_at_all_the_fallback_is_normalised_rrf_not_zero() -> None:
    scored = score_relevance(
        [A, B], semantic_scores={}, lexical_scores=None, rrf_scores={A: 0.016, B: 0.008}
    )

    assert scored[A].relevance == pytest.approx(1.0)
    assert scored[B].relevance == pytest.approx(0.5)
    assert set(scored[A].parts) == {"rrf_normalised"}


def test_an_empty_lexicon_is_falsy_so_callers_skip_the_coverage_statement() -> None:
    assert not QueryLexicon()
    assert not QueryLexicon(lexemes=("the",), idf={"the": 0.0}, corpus_size=10)
    assert QueryLexicon(lexemes=("tika",), idf={"tika": 6.1}, corpus_size=4757)


def test_total_idf_is_the_denominator_of_coverage() -> None:
    lexicon = QueryLexicon(lexemes=("mars", "plan"), idf={"mars": 8.5, "plan": 0.9}, corpus_size=4757)

    # A candidate covering only the common term covers ~10 % of the query's IDF mass: the reason a
    # question about something the corpus does not contain cannot score like one it does.
    assert lexicon.idf["plan"] / lexicon.total_idf == pytest.approx(0.0957, abs=1e-3)
