"""Unit tests for :mod:`tests.evaluation.scorer` against synthetic
:class:`~aimemory.domain.retrieval.SearchResult` objects, plus a shape check on
``tests/evaluation/gold.yaml`` itself (P2-T04 groundwork for P14-T03, owner A12).

Deliberately needs no service: the whole point of ``scorer.py`` is that it can be exercised before
P9 (retrieval) exists. Marked ``evaluation`` for the marker-based runs (``pytest -m evaluation``) even
though it is, in fact, a fast pure-function unit test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from aimemory.domain.enums import ObjectType
from aimemory.domain.provenance import Provenance
from aimemory.domain.retrieval import ScoredHit, SearchQuery, SearchResult
from aimemory.domain.source_uri import parse_source_uri

# ``tests/evaluation`` has no ``__init__.py`` (matching the rest of this suite - see tests/README.md),
# so pytest's default "prepend" import mode puts this directory on sys.path itself; a plain top-level
# import of the sibling module is therefore correct here, not a bare-import mistake.
from scorer import (
    GoldQuestion,
    aggregate_scores,
    load_gold_set,
    score_entity_presence,
    score_expected_types,
    score_facts_contain,
    score_hit_at_k,
    score_provenance_completeness,
    score_question,
    score_temporal_order,
    timeline_order_correct,
)

pytestmark = pytest.mark.evaluation

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_YAML = REPO_ROOT / "tests" / "evaluation" / "gold.yaml"
DEVICE_ID = "local-development-machine"


def _prov(*, source_uri: str | None = None, complete: bool = True, valid_from: datetime | None = None) -> Provenance:
    return Provenance(
        device_id=DEVICE_ID,
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
        source_uri=source_uri,
        source_id=uuid4() if complete else None,
        source_version=uuid4() if complete else None,
        valid_from=valid_from,
    )


def _hit(
    *,
    text: str = "some evidence text",
    title: str | None = None,
    source_uri: str | None = "vault://my-vault/AIOS/me.md",
    complete: bool = True,
    valid_from: datetime | None = None,
) -> ScoredHit:
    return ScoredHit(
        object_type=ObjectType.CHUNK,
        object_id=uuid4(),
        title=title,
        text=text,
        score=1.0,
        provenance=_prov(source_uri=source_uri, complete=complete, valid_from=valid_from),
    )


def _result(hits: list[ScoredHit]) -> SearchResult:
    return SearchResult(query=SearchQuery(query="what is JobPilot?"), hits=hits)


# ---- score_hit_at_k --------------------------------------------------------------------------


def test_hit_at_k_none_when_question_makes_no_source_claim() -> None:
    assert score_hit_at_k(_result([_hit()]), []) is None


def test_hit_at_k_true_when_expected_source_in_top_k() -> None:
    result = _result([_hit(source_uri="vault://my-vault/AIOS/me.md")])
    assert score_hit_at_k(result, ["vault://my-vault/AIOS/me.md"], k=5) is True


def test_hit_at_k_false_when_expected_source_absent() -> None:
    result = _result([_hit(source_uri="vault://my-vault/other.md")])
    assert score_hit_at_k(result, ["vault://my-vault/AIOS/me.md"], k=5) is False


def test_hit_at_k_ignores_hits_beyond_k() -> None:
    hits = [_hit(source_uri=f"vault://my-vault/n{i}.md") for i in range(5)]
    hits.append(_hit(source_uri="vault://my-vault/AIOS/me.md"))  # rank 6, outside top-5
    assert score_hit_at_k(_result(hits), ["vault://my-vault/AIOS/me.md"], k=5) is False
    assert score_hit_at_k(_result(hits), ["vault://my-vault/AIOS/me.md"], k=6) is True


# ---- score_entity_presence -------------------------------------------------------------------


def test_entity_presence_none_when_no_expected_entities() -> None:
    assert score_entity_presence(_result([_hit()]), []) is None


def test_entity_presence_partial_credit() -> None:
    result = _result([_hit(text="JobPilot is built with SvelteKit.")])
    presence = score_entity_presence(result, ["JobPilot", "SvelteKit", "Convex"])
    assert presence == pytest.approx(2 / 3)


def test_entity_presence_is_case_insensitive() -> None:
    result = _result([_hit(text="jobpilot uses sveltekit")])
    assert score_entity_presence(result, ["JobPilot", "SvelteKit"]) == 1.0


# ---- score_expected_types --------------------------------------------------------------------


def test_expected_types_matches_object_type() -> None:
    hit = ScoredHit(
        object_type=ObjectType.ARTIFACT, object_id=uuid4(), text="a decision", score=1.0,
        provenance=_prov(),
    )
    assert score_expected_types(_result([hit]), ["artifact"]) == 1.0


def test_expected_types_none_when_no_expected_types() -> None:
    assert score_expected_types(_result([_hit()]), []) is None


# ---- score_facts_contain ---------------------------------------------------------------------


def test_facts_contain_finds_substring() -> None:
    result = _result([_hit(text="Personal Harness is currently parked.")])
    assert score_facts_contain(result, ["parked"]) == 1.0


def test_facts_contain_missing_substring_scores_zero() -> None:
    result = _result([_hit(text="Personal Harness is active.")])
    assert score_facts_contain(result, ["parked"]) == 0.0


# ---- score_provenance_completeness -------------------------------------------------------------


def test_provenance_completeness_full_when_all_hits_complete() -> None:
    result = _result([_hit(complete=True), _hit(complete=True)])
    assert score_provenance_completeness(result) == 1.0


def test_provenance_completeness_partial() -> None:
    result = _result([_hit(complete=True), _hit(complete=False)])
    assert score_provenance_completeness(result) == pytest.approx(0.5)


def test_provenance_completeness_vacuously_complete_with_no_hits() -> None:
    assert score_provenance_completeness(_result([])) == 1.0


# ---- timeline / temporal correctness -----------------------------------------------------------


def test_timeline_order_correct_true_for_correctly_ordered_events() -> None:
    events = [
        ("Architecture A selected", datetime(2026, 9, 1, tzinfo=UTC)),
        ("Architecture A abandoned", datetime(2026, 9, 10, tzinfo=UTC)),
        ("Architecture B selected", datetime(2026, 9, 11, tzinfo=UTC)),
    ]
    expected = ["Architecture A selected", "Architecture A abandoned", "Architecture B selected"]
    assert timeline_order_correct(events, expected) is True


def test_timeline_order_correct_false_when_reversed() -> None:
    events = [
        ("Architecture B selected", datetime(2026, 9, 1, tzinfo=UTC)),
        ("Architecture A selected", datetime(2026, 9, 11, tzinfo=UTC)),
    ]
    assert timeline_order_correct(events, ["Architecture A selected", "Architecture B selected"]) is False


def test_score_temporal_order_none_when_question_makes_no_claim() -> None:
    assert score_temporal_order(_result([_hit()]), []) is None


def test_score_temporal_order_from_result_hits() -> None:
    hits = [
        _hit(title="Architecture A selected", text="...", valid_from=datetime(2026, 9, 1, tzinfo=UTC)),
        _hit(title="Architecture B selected", text="...", valid_from=datetime(2026, 9, 11, tzinfo=UTC)),
    ]
    result = _result(hits)
    order = ["Architecture A selected", "Architecture B selected"]
    assert score_temporal_order(result, order) is True


# ---- score_question / aggregate_scores ----------------------------------------------------------


def test_score_question_end_to_end() -> None:
    question = GoldQuestion(
        id="Q_TEST",
        question="What is JobPilot?",
        expected_sources_any=("vault://my-vault/AIOS/me.md",),
        expected_entities=("JobPilot", "SvelteKit"),
    )
    result = _result([_hit(source_uri="vault://my-vault/AIOS/me.md", text="JobPilot uses SvelteKit.")])
    score = score_question(question, result)
    assert score.hit_at_5 is True
    assert score.entity_presence == 1.0
    assert score.provenance_completeness == 1.0


def test_aggregate_scores_computes_means_and_ignores_none() -> None:
    q1 = score_question(
        GoldQuestion(id="Q1", question="?", expected_sources_any=("vault://a",)),
        _result([_hit(source_uri="vault://a")]),
    )
    q2 = score_question(
        GoldQuestion(id="Q2", question="?", expected_entities=("X",)),
        _result([_hit(text="no match here")]),
    )
    summary = aggregate_scores([q1, q2])
    assert summary["n_questions"] == 2
    assert summary["hit_at_5_rate"] == 1.0
    assert summary["hit_at_5_n"] == 1
    assert summary["entity_presence_mean"] == 0.0
    assert summary["provenance_completeness_is_100pct"] is True


# ---- gold.yaml shape --------------------------------------------------------------------------


def test_gold_yaml_loads_and_has_a_sane_shape() -> None:
    version, questions = load_gold_set(GOLD_YAML)
    assert version
    assert len(questions) >= 1

    ids = [q.id for q in questions]
    assert len(ids) == len(set(ids)), "duplicate question ids in gold.yaml"

    for question in questions:
        assert question.question.strip(), f"{question.id}: empty question text"
        assert question.author in {"owner", "agent"}, f"{question.id}: author must be 'owner' or 'agent'"
        assert not question.makes_no_checkable_claim, (
            f"{question.id}: makes no checkable claim (no expected_* field set) - "
            "either add one or this row cannot be scored"
        )
        for uri in question.expected_sources_any:
            parse_source_uri(uri)  # raises SourceUriError on a malformed URI


def test_gold_yaml_is_marked_as_agent_extensible_with_owner_authored_seed_questions() -> None:
    """Acceptance detail from the task: the owner's seed questions must be present and marked
    ``author: owner``; anything A12 adds later must be marked ``author: agent`` (see gold.yaml's own
    header comment) so the owner knows which rows to double-check."""
    _, questions = load_gold_set(GOLD_YAML)
    owner_questions = {q.question for q in questions if q.author == "owner"}
    expected_owner_questions = {
        "What is JobPilot?",
        "Which projects use Databricks?",
        "Which projects relate to optimization?",
    }
    missing = expected_owner_questions - owner_questions
    assert not missing, f"owner's seed questions missing from gold.yaml: {missing}"
