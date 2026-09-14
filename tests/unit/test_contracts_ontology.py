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
    assert onto.version == "0.1.0"
    assert len(onto.node_types) == 19, "19 node types (SubProject is stored as Project)"
    assert len(onto.stored_labels) == 18, "plan section H: 18 distinct Neo4j labels"
    assert len(onto.relationship_types) == 19, "plan section H: 19 relationship types"


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
        "HAS_OWNER",
        "USES_ARCHITECTURE",
        "DEPLOYED_ON",
        "HAS_STAGE",
        "SELECTED_OPTION",
    ]


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
    onto.validate_relationship(Predicate.HAS_STATUS, "Project", "Project")  # functional: no limit
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
    raw["functional_predicates"].append("HAS_VIBE")
    broken = tmp_path / "ontology.yaml"
    broken.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(OntologyError):
        _load_file(broken)


def test_loader_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OntologyError):
        _load_file(tmp_path / "nope.yaml")


def test_technology_alias_seed_file_exists() -> None:
    onto = load_ontology(ONTOLOGY_FILE)
    seed = onto.node_types["Technology"].aliases_seed
    assert seed == "config/technology-aliases.yaml"
    assert (REPO_ROOT / seed).exists()
