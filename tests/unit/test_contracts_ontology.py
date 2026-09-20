"""P1-T01: the ontology loader validates ``schemas/ontology.yaml`` against the domain contracts.

Owner: A02. Consumers of the guarantees tested here: A04 (Neo4j constraints), A08 (temporal rules,
projection), A09 (graph expansion), A11 (dashboard queries).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from aimemory.common.errors import OntologyError
from aimemory.domain.enums import (
    ArtifactStatus,
    ArtifactType,
    EntityType,
    EpisodeType,
    Predicate,
    RelationshipGroup,
    Track,
)
from aimemory.ontology import load_ontology
from aimemory.ontology.loader import _load_file, ontology_path

REPO_ROOT = Path(__file__).resolve().parents[2]
ONTOLOGY_FILE = REPO_ROOT / "schemas" / "ontology.yaml"
CONSTRAINTS_FILE = REPO_ROOT / "infra" / "neo4j" / "schema" / "constraints.cypher"


def test_default_path_points_at_the_repository_contract() -> None:
    assert ontology_path().name == "ontology.yaml"
    assert ontology_path().exists()


def test_ontology_loads_and_validates() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    assert onto.version == "0.2.0", "ADR-0015 changed predicate cardinality and direction"
    assert len(onto.node_types) == 19, "19 node types (SubProject is stored as Project)"
    assert len(onto.stored_labels) == 18, "plan section H: 18 distinct Neo4j labels"
    assert len(onto.relationship_types) == 22, "19 from plan section H + the 3 ADR-0015 moved"


def test_labels_match_entity_type_enum() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    assert set(onto.labels) == {t.value for t in EntityType}


def test_predicates_match_predicate_enum() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    from_file = set(onto.relationship_types) | {p.value for p in onto.functional_predicates}
    assert from_file == {p.value for p in Predicate}


def test_relationship_groups_are_complete() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    groups = {spec.group for spec in onto.relationship_types.values()}
    assert groups == set(RelationshipGroup)


def test_functional_predicates_drive_adr_0005_rule_1() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    assert onto.is_functional("HAS_STATUS")
    assert onto.is_functional(Predicate.SELECTED_OPTION)
    assert not onto.is_functional(Predicate.USES), "non-functional predicates accumulate"
    assert not onto.is_functional(Predicate.RELATED_TO)
    assert [p.value for p in onto.functional_predicates] == [
        "HAS_STATUS",
        "HAS_STAGE",
        "SELECTED_OPTION",
    ]


def test_multi_valued_predicates_are_not_functional() -> None:
    """ADR-0015: the defect these three caused was silent and data-destroying.

    MEASURED on the 2026-09-17 vault corpus: `JobLab USES_ARCHITECTURE` had 5 current and 13
    historical objects because every newly extracted technology closed the previous one, so
    `Python` and `PostgreSQL` - concurrently true - were answered as history.
    """
    onto = load_ontology(ONTOLOGY_FILE)
    for predicate in (Predicate.USES_ARCHITECTURE, Predicate.HAS_OWNER, Predicate.DEPLOYED_ON):
        assert not onto.is_functional(predicate), f"{predicate} is multi-valued"
        assert onto.relationship(predicate) is not None, f"{predicate} must be a declared edge type"
        assert onto.group_of(predicate) is RelationshipGroup.SEMANTIC


def test_every_functional_predicate_records_why_it_is_single_valued() -> None:
    """The membership test is the point of ADR-0015, so it has to be written down per predicate."""
    onto = load_ontology(ONTOLOGY_FILE)
    for name, spec in onto.functional_specs.items():
        assert spec.reason, f"{name} must state why two concurrent values are a contradiction"
        assert spec.subjects, f"{name} has no node type declaring it in `functional:`"
        assert spec.to_labels, f"{name} must declare what its object may be"


def test_functional_subjects_come_from_the_node_types() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    assert "Project" in onto.functional_subjects("HAS_STATUS")
    assert "Person" not in onto.functional_subjects("HAS_STATUS"), (
        "a Person has no status in this ontology; `Mohammad HAS_STATUS Picnic` was a mis-typing "
        "of an application, and it was written 7/7 times before ADR-0015"
    )
    assert onto.functional_subjects("SELECTED_OPTION") == [
        "Project",
        "SubProject",
        "Decision",
        "Requirement",
        "Task",
        "Experiment",
    ]


def test_has_status_object_must_be_a_literal() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    onto.validate_relationship(Predicate.HAS_STATUS, "Project", None)  # literal object: ok
    with pytest.raises(OntologyError):
        onto.validate_relationship(Predicate.HAS_STATUS, "Project", "Technology")
    with pytest.raises(OntologyError):
        onto.validate_relationship(Predicate.HAS_STATUS, "Person", None)
    with pytest.raises(OntologyError, match="functional"):
        onto.validate_relationship(Predicate.USES, "Project", None)


def test_orient_flips_a_reversed_triple_only_when_it_is_unambiguous() -> None:
    """ADR-0015 direction repair. MEASURED: 32 of 58 `HAS_OWNER` facts were written backwards."""
    onto = load_ontology(ONTOLOGY_FILE)

    kept = onto.orient(Predicate.HAS_OWNER, "Repository", "Person")
    assert (kept.from_label, kept.to_label, kept.flipped) == ("Repository", "Person", False)

    flipped = onto.orient(Predicate.HAS_OWNER, "Person", "Project")
    assert (flipped.from_label, flipped.to_label, flipped.flipped) == ("Project", "Person", True)
    assert flipped.reason

    # Legal both ways -> the ontology cannot tell, so it changes nothing.
    ambiguous = onto.orient(Predicate.HAS_OWNER, "Person", "Person")
    assert ambiguous.flipped is False

    # Illegal both ways -> raise, never guess.
    with pytest.raises(OntologyError):
        onto.orient(Predicate.STORED_ON, "Project", "Technology")


def test_subproject_is_stored_as_project() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    assert onto.stored_label(EntityType.SUB_PROJECT) == "Project"
    assert onto.stored_label(EntityType.TECHNOLOGY) == "Technology"
    assert "SubProject" not in onto.stored_labels


def test_stored_labels_match_the_neo4j_constraints_file() -> None:
    """A04 owns constraints.cypher; it must declare one uniqueness constraint per stored label."""
    onto = load_ontology(ONTOLOGY_FILE)
    text = CONSTRAINTS_FILE.read_text(encoding="utf-8")
    declared = set(re.findall(r"FOR \(n:(\w+)\) REQUIRE n\.id IS UNIQUE", text))
    assert declared == set(onto.stored_labels)


def test_relationship_endpoints_are_validated() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    onto.validate_relationship(Predicate.USES, "Project", "Technology")
    onto.validate_relationship(Predicate.MENTIONS, "Document", "Person")  # wildcard target
    onto.validate_relationship(Predicate.HAS_STAGE, "Project", None)  # functional, literal object
    with pytest.raises(OntologyError):
        # Before ADR-0015 a functional predicate accepted any endpoints at all.
        onto.validate_relationship(Predicate.HAS_STATUS, "Project", "Project")
    with pytest.raises(OntologyError):
        onto.validate_relationship(Predicate.STORED_ON, "Project", "Technology")
    with pytest.raises(OntologyError):
        onto.validate_relationship("NOT_A_PREDICATE", "Project", "Technology")


def test_property_contracts_are_machine_readable() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    assert onto.node_properties.required == ["id", "name", "type", "observed_at", "engine"]
    assert "valid_from" in onto.node_properties.all_properties
    assert onto.relationship_properties.required == [
        "fact_id",
        "valid_from",
        "observed_at",
        "engine",
    ]
    assert onto.node_properties.missing({"id": "x", "name": "y"}) == [
        "type",
        "observed_at",
        "engine",
    ]


def test_vocabularies_match_the_enums() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    assert {t.value for t in onto.artifact_types} == {t.value for t in ArtifactType}
    assert {s.value for s in onto.artifact_status} == {s.value for s in ArtifactStatus}
    assert {e.value for e in onto.episode_types} == {e.value for e in EpisodeType}
    assert {t.value for t in onto.tracks} == {t.value for t in Track}


def test_loader_rejects_an_inconsistent_ontology(tmp_path: Path) -> None:
    raw = yaml.safe_load(ONTOLOGY_FILE.read_text(encoding="utf-8"))
    raw["node_types"].pop("Dataset")
    broken = tmp_path / "ontology.yaml"
    broken.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(OntologyError) as excinfo:
        _load_file(broken)
    assert "Ontology" in str(excinfo.value)
    assert "Dataset" in (excinfo.value.detail or "")


def test_loader_rejects_an_unknown_predicate(tmp_path: Path) -> None:
    raw = yaml.safe_load(ONTOLOGY_FILE.read_text(encoding="utf-8"))
    raw["functional_predicates"]["HAS_VIBE"] = {"to": ["literal"], "reason": "not a predicate"}
    broken = tmp_path / "ontology.yaml"
    broken.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(OntologyError):
        _load_file(broken)


def test_loader_rejects_a_functional_predicate_no_node_type_claims(tmp_path: Path) -> None:
    """A functional predicate with no subject would reject every fact that used it, silently."""
    raw = yaml.safe_load(ONTOLOGY_FILE.read_text(encoding="utf-8"))
    for node in raw["node_types"].values():
        node["functional"] = [p for p in node.get("functional", []) if p != "HAS_STAGE"]
    broken = tmp_path / "ontology.yaml"
    broken.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(OntologyError, match="Ontology"):
        _load_file(broken)


def test_loader_rejects_a_node_type_claiming_a_non_functional_predicate(tmp_path: Path) -> None:
    """The pre-ADR-0015 file said `Project: functional: [HAS_STATUS, HAS_OWNER]`; that must fail."""
    raw = yaml.safe_load(ONTOLOGY_FILE.read_text(encoding="utf-8"))
    raw["node_types"]["Project"]["functional"].append("HAS_OWNER")
    broken = tmp_path / "ontology.yaml"
    broken.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(OntologyError, match="Ontology"):
        _load_file(broken)


def test_loader_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OntologyError):
        _load_file(tmp_path / "nope.yaml")


def test_technology_alias_seed_file_exists() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    seed = onto.node_types["Technology"].aliases_seed
    assert seed == "config/technology-aliases.yaml"
    assert (REPO_ROOT / seed).exists()
