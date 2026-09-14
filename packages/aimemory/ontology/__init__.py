"""``aimemory.ontology`` - typed access to ``schemas/ontology.yaml``.

Owned by A02; frozen after P1 together with ``schemas/`` (changes need an ADR).
Consumers: A04 (Neo4j constraints), A05/A08 (prompt vocabularies, temporal rules, projection),
A09 (graph expansion), A11 (dashboards), A12 (tests).

Typical use::

    from aimemory.ontology import load_ontology, is_functional

    onto = load_ontology()                 # parsed, validated against the domain enums, cached
    onto.stored_label("SubProject")        # -> "Project"
    is_functional("HAS_STATUS")            # -> True   (ADR-0005 rule 1)
    onto.validate_relationship("USES", "Project", "Technology")
"""

from .loader import (
    WILDCARD,
    NodeTypeSpec,
    Ontology,
    PropertyContract,
    RelationshipTypeSpec,
    is_functional,
    load_ontology,
    ontology_path,
)

__all__ = [
    "WILDCARD",
    "NodeTypeSpec",
    "Ontology",
    "PropertyContract",
    "RelationshipTypeSpec",
    "is_functional",
    "load_ontology",
    "ontology_path",
]
