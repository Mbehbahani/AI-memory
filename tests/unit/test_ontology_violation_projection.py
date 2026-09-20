"""Edges whose endpoint types break the ontology: dropped, or kept and marked.

The problem
-----------
`Ontology.validate_relationship` enforces ADR-0015's endpoint contract, and the projection used to
respond to a violation by discarding the edge. MEASURED 2026-09-19 on the real corpus: **676 of
5,256 semantic edges (13 %)** were refused that way - `Person -[HAS_OWNER]-> Project` (backwards),
`Concept -[PART_OF]-> Concept` (not an allowed pair), and about 160 other shapes.

The facts were still in Postgres, so nothing was lost; they were simply invisible in the graph. That
is the worst place for them: invisible knowledge cannot be reviewed, cannot be repaired, and shows up
only as a graph that looks thin for no discoverable reason.

The rule these tests hold
-------------------------
Under ``GRAPH_ALLOW_ONTOLOGY_VIOLATIONS`` the edge is written, carrying ``ontology_violation`` naming
the rule it breaks. **Marked, not laundered.** One predicate excludes them; nothing anywhere claims a
backwards edge is well formed. The default stays strict, so this is a decision an operator makes
rather than one that happens quietly.
"""

from __future__ import annotations

from datetime import timezone, datetime
from uuid import uuid4

import pytest

from aimemory.domain.enums import EntityType
from aimemory.domain.models import Fact
from aimemory.providers.graph.projection import fact_edges

# `Person -[HAS_OWNER]-> Project` is backwards: ownership runs Project -> Person. It was the single
# largest violation shape in the real corpus (22 facts), so it is the one worth pinning a test to.
BACKWARDS = ("HAS_OWNER", EntityType.PERSON, EntityType.PROJECT)


AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _fact(predicate: str) -> Fact:
    from aimemory.domain.provenance import Provenance

    return Fact(
        id=uuid4(),
        subject_entity_id=uuid4(),
        predicate=predicate,
        object_entity_id=uuid4(),
        statement="Mohammad owns the JobLab lakehouse.",
        valid_from=AT,
        observed_at=AT,
        provenance=Provenance(device_id="local-development-machine", observed_at=AT),
    )


def _labels(fact: Fact, subject: EntityType, obj: EntityType) -> dict[str, EntityType]:
    return {str(fact.subject_entity_id): subject, str(fact.object_entity_id): obj}


@pytest.fixture()
def violating():
    predicate, subject, obj = BACKWARDS
    fact = _fact(predicate)
    return fact, _labels(fact, subject, obj)


# ------------------------------------------------------------------ strict: the default


def test_by_default_a_violating_edge_is_dropped_with_a_reason(violating) -> None:
    """Unchanged behaviour. Relaxing this must be a decision, never a side effect."""
    fact, labels = violating

    plan = fact_edges([fact], labels=labels, allow_ontology_violations=False)

    assert plan.relationships == []
    assert len(plan.dropped) == 1
    assert "violate the ontology" in plan.dropped[0][1]


# ------------------------------------------------------------------ permissive: kept and marked


def test_permissive_mode_keeps_the_edge(violating) -> None:
    fact, labels = violating

    plan = fact_edges([fact], labels=labels, allow_ontology_violations=True)

    assert plan.dropped == []
    assert len(plan.relationships) == 1


def test_the_kept_edge_says_which_rule_it_breaks(violating) -> None:
    """The whole difference between keeping and laundering.

    Without the marker the graph would assert `Person -[HAS_OWNER]-> Project` as an ordinary fact and
    nothing downstream could tell it apart from a correct one.
    """
    fact, labels = violating

    plan = fact_edges([fact], labels=labels, allow_ontology_violations=True)

    violation = plan.relationships[0].properties.get("ontology_violation")
    assert violation, "a kept violation must name itself"
    assert "HAS_OWNER" in violation
    assert "Person" in violation and "Project" in violation


def test_a_valid_edge_is_never_marked() -> None:
    """If every edge carried the marker, excluding on it would exclude the whole graph."""
    fact = _fact("USES")
    labels = _labels(fact, EntityType.PROJECT, EntityType.TECHNOLOGY)

    plan = fact_edges([fact], labels=labels, allow_ontology_violations=True)

    assert plan.dropped == [], plan.dropped
    assert len(plan.relationships) == 1
    assert "ontology_violation" not in plan.relationships[0].properties


# ------------------------------------------------------------------ what permissiveness does NOT do


def test_a_missing_endpoint_is_still_dropped(violating) -> None:
    """Permissive is about *type* violations. An endpoint with no node cannot be written at all."""
    fact, _ = violating

    plan = fact_edges([fact], labels={}, allow_ontology_violations=True)

    assert plan.relationships == []
    assert plan.dropped and "not projected" in plan.dropped[0][1]


def test_the_setting_defaults_to_strict() -> None:
    """A fresh install keeps the ontology contract; the operator opts out deliberately.

    Asserted against the *declared* default rather than an instance: this machine's `.env` enables
    it, and pydantic-settings reads that file directly, so instantiating here would only prove what
    the local environment happens to say. The declared default is what a fresh clone gets.
    """
    from aimemory.common.config import Neo4jSettings

    assert Neo4jSettings.model_fields["allow_ontology_violations"].default is False


# ------------------------------------------------------------------ the related equivalence rule


def test_two_routes_to_the_same_model_are_not_a_mixed_corpus() -> None:
    """ADR-0014 rule 2 is about models that disagree, not about billing paths.

    `qwen3:4b` and Haiku 4.5 classify entities differently, so mixing them really does corrupt a
    corpus. Bedrock Haiku 4.5 and the ADR-0016 relay are the same weights at temperature 0. Treating
    them as different would have flagged 1,245 facts `unconfirmed` - the full cost of a model change
    with none of the cause.
    """
    from aimemory.persistence.ingest_repo import equivalent_model_ids

    both = equivalent_model_ids("claude-code:haiku-4-5")

    assert "bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0" in both
    assert equivalent_model_ids("bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0") == both
    assert equivalent_model_ids("qwen3-4b") == ["qwen3-4b"], "a genuinely different model stands alone"


def test_the_extraction_guard_also_honours_model_equivalence() -> None:
    """Three guards implement ADR-0014 rule 2, and all three must agree.

    Found 2026-09-20: `flag_other_model_facts` and `extraction_models_in_use` were taught about
    equivalent routes, but `assert_single_extraction_model` still compared ids directly. The result
    was a corpus that reported one model in use and then refused to extract into it - the guard
    blocking the very write its own status said was fine.
    """
    import inspect

    from aimemory.sources import tier2

    source = inspect.getsource(tier2.assert_single_extraction_model)

    assert "equivalent_model_ids" in source, (
        "the refusal must compare against every id naming the same model, not just the one being "
        "written, or a route change demands a full re-extraction that changes nothing"
    )
