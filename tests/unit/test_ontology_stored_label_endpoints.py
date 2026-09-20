"""Regression tests: endpoint validation must compare stored labels on *both* sides.

`SubProject` is declared `stored_as: Project` in `schemas/ontology.yaml` - a sub-project is a Project
row with a `parent_id`, written to Neo4j as a plain `:Project` node. `Ontology.validate_relationship`
maps the incoming labels through `stored_label()`, but used to compare the result against the spec's
*raw* endpoint lists. So `SubProject` arrived as `Project`, PART_OF's `from: [SubProject, Document,
Task, Requirement]` does not contain `Project`, and the edge the ontology plainly intends to allow
could never validate.

MEASURED consequence before the fix: 7 extracted project-hierarchy facts were silently dropped by the
Neo4j projection. It did not break the *structural* hierarchy only because the registry happens to
hold 0 sub-projects today - it would have the moment one existed.
"""

from __future__ import annotations

import pytest
from aimemory.common.errors import OntologyError
from aimemory.ontology.loader import load_ontology


@pytest.fixture(scope="module")
def ontology():
    return load_ontology()


def test_subproject_is_stored_as_project(ontology) -> None:
    """The precondition the bug depended on - if this ever changes, the tests below are about
    nothing and should be revisited rather than silently kept passing."""
    assert ontology.stored_label("SubProject") == "Project"


def test_a_subproject_may_be_part_of_a_project(ontology) -> None:
    """The edge the ontology declares and the validator used to refuse."""
    ontology.validate_relationship("PART_OF", "SubProject", "Project")


def test_project_to_project_part_of_is_accepted_because_the_labels_are_identical(ontology) -> None:
    """The unavoidable consequence of `stored_as`, asserted so it is a recorded decision and not an
    accident: a validator sees `:Project` for both a project and a sub-project, so it must accept
    both or reject both. What distinguishes them is `parent_id`, not the label."""
    ontology.validate_relationship("PART_OF", "Project", "Project")


def test_document_part_of_project_still_works(ontology) -> None:
    """A plain endpoint with no `stored_as` alias is unaffected by the normalisation."""
    ontology.validate_relationship("PART_OF", "Document", "Project")


def test_the_rules_are_still_enforced(ontology) -> None:
    """The fix must not turn endpoint validation into a rubber stamp."""
    with pytest.raises(OntologyError):
        ontology.validate_relationship("PART_OF", "Person", "Technology")
    with pytest.raises(OntologyError):
        ontology.validate_relationship("STORED_ON", "Project", "Technology")


def test_wildcard_endpoints_are_unaffected(ontology) -> None:
    """`MENTIONS` has `to: ["*"]`; the mapping must pass the wildcard through, not try to resolve it."""
    ontology.validate_relationship("MENTIONS", "Document", "Technology")
    ontology.validate_relationship("MENTIONS", "Episode", "Person")


def test_an_unknown_predicate_is_still_rejected(ontology) -> None:
    with pytest.raises(OntologyError):
        ontology.validate_relationship("NOT_A_REAL_PREDICATE", "Project", "Project")
