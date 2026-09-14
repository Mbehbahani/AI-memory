"""Typed loader and validator for ``schemas/ontology.yaml``.

Consumers: A04 (generates ``infra/neo4j/schema/constraints.cypher`` labels and checks them),
A05/A08 (builds the allowed label/predicate lists put into extraction prompts and into
``schemas/extraction/*.json``), A08 (temporal rules ask :func:`is_functional`; the projection asks
:meth:`Ontology.stored_label` and :meth:`Ontology.validate_relationship`), A09 (graph expansion
restricts to known relationship types), A11 (dashboard queries), A12 (tests).

Contract guarantees, all enforced at load time by :meth:`Ontology.validate_contracts`:

* every :class:`~aimemory.domain.enums.EntityType` member exists in ``node_types`` and vice versa;
* every :class:`~aimemory.domain.enums.Predicate` member is either a relationship type or a
  functional predicate, and vice versa;
* every ``from``/``to`` label in a relationship definition is a known label or the wildcard ``"*"``;
* ``functional`` lists on node types name known predicates;
* ``artifact_types``, ``artifact_status``, ``episode_types`` and ``tracks`` match
  :class:`ArtifactType`, :class:`ArtifactStatus`, :class:`EpisodeType` and :class:`Track`.

A violation raises :class:`~aimemory.common.errors.OntologyError`, so a bad ontology file fails at
import of the service rather than halfway through an ingestion run.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from ..common.config import default_schema_dir
from ..common.errors import OntologyError
from ..domain.base import DomainModel
from ..domain.enums import (
    ArtifactStatus,
    ArtifactType,
    EntityType,
    EpisodeType,
    Predicate,
    RelationshipGroup,
    Track,
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

#: ``"*"`` in a ``from``/``to`` list means "any label" (``MENTIONS``, ``RELATED_TO``, ...).
WILDCARD = "*"


class NodeTypeSpec(DomainModel):
    """One entry of ``node_types``.

    ``stored_as`` names the label actually written to Neo4j when it differs from the type name -
    only ``SubProject`` uses it (stored as ``Project`` with ``parent_id``), which is why the ontology
    declares 19 node types but ``constraints.cypher`` creates 18 uniqueness constraints.
    """

    name: str
    description: str = ""
    functional: list[Predicate] = Field(default_factory=list)
    aliases_seed: str | None = None
    stored_as: str | None = None

    @property
    def stored_label(self) -> str:
        """The Neo4j label for this type."""
        return self.stored_as or self.name


class RelationshipTypeSpec(DomainModel):
    """One entry of ``relationship_types.<group>``, with its allowed endpoint labels."""

    name: str
    group: RelationshipGroup
    from_labels: list[str] = Field(default_factory=list)
    to_labels: list[str] = Field(default_factory=list)

    def allows(self, from_label: str, to_label: str) -> bool:
        """True when both endpoints are permitted (``"*"`` permits anything)."""
        ok_from = WILDCARD in self.from_labels or from_label in self.from_labels
        ok_to = WILDCARD in self.to_labels or to_label in self.to_labels
        return ok_from and ok_to


class PropertyContract(DomainModel):
    """``node_properties`` / ``relationship_properties``: which keys must be present on write."""

    required: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)

    @property
    def all_properties(self) -> list[str]:
        return [*self.required, *self.optional]

    def missing(self, payload: dict[str, Any]) -> list[str]:
        """Required keys that are absent or ``None`` in ``payload``."""
        return [key for key in self.required if payload.get(key) is None]


class Ontology(DomainModel):
    """The parsed, validated ontology. Build it with :func:`load_ontology` (cached)."""

    version: str
    node_types: dict[str, NodeTypeSpec]
    relationship_types: dict[str, RelationshipTypeSpec]
    functional_predicates: list[Predicate]
    node_properties: PropertyContract
    relationship_properties: PropertyContract
    artifact_types: list[ArtifactType]
    artifact_status: list[ArtifactStatus]
    episode_types: list[EpisodeType]
    tracks: list[Track]
    source_path: Path | None = None

    # ---- lookups -------------------------------------------------------------------------------

    @property
    def labels(self) -> list[str]:
        """All node type names, including ``SubProject``."""
        return list(self.node_types)

    @property
    def stored_labels(self) -> list[str]:
        """Distinct Neo4j labels - what ``constraints.cypher`` must cover (18 in V0.1)."""
        seen: list[str] = []
        for spec in self.node_types.values():
            if spec.stored_label not in seen:
                seen.append(spec.stored_label)
        return seen

    def stored_label(self, label: EntityType | str) -> str:
        """Map an entity type to the label actually written to Neo4j."""
        name = label.value if isinstance(label, EntityType) else str(label)
        spec = self.node_types.get(name)
        if spec is None:
            raise OntologyError("Unknown node label.", detail=f"label={name!r}")
        return spec.stored_label

    def is_functional(self, predicate: Predicate | str) -> bool:
        """ADR-0005 rule 1: does ``(subject, predicate)`` admit only one current object?"""
        name = predicate.value if isinstance(predicate, Predicate) else str(predicate)
        return name in {p.value for p in self.functional_predicates}

    def relationship(self, predicate: Predicate | str) -> RelationshipTypeSpec | None:
        """The edge spec, or ``None`` for a functional predicate (which is not an edge type)."""
        name = predicate.value if isinstance(predicate, Predicate) else str(predicate)
        return self.relationship_types.get(name)

    def group_of(self, predicate: Predicate | str) -> RelationshipGroup | None:
        spec = self.relationship(predicate)
        return spec.group if spec else None

    def validate_relationship(
        self, predicate: Predicate | str, from_label: str, to_label: str
    ) -> None:
        """Raise :class:`OntologyError` if this edge is not allowed between these labels."""
        spec = self.relationship(predicate)
        if spec is None:
            if self.is_functional(predicate):
                return  # functional predicates are attribute-like; endpoints are not constrained
            raise OntologyError("Unknown relationship type.", detail=f"predicate={predicate!r}")
        if not spec.allows(self.stored_label(from_label), self.stored_label(to_label)):
            raise OntologyError(
                "Relationship endpoints violate the ontology.",
                detail=f"{from_label} -[{spec.name}]-> {to_label}",
            )

    # ---- validation ----------------------------------------------------------------------------

    def validate_contracts(self) -> Ontology:
        """Cross-check the file against the domain enums. Called by :func:`load_ontology`.

        Named ``validate_contracts`` rather than ``validate`` so it cannot shadow pydantic's own
        deprecated ``BaseModel.validate`` classmethod.
        """
        problems: list[str] = []

        file_labels = set(self.node_types)
        enum_labels = {t.value for t in EntityType}
        if file_labels != enum_labels:
            problems.append(
                f"node_types vs EntityType mismatch: only-in-file={sorted(file_labels - enum_labels)}, "
                f"only-in-code={sorted(enum_labels - file_labels)}"
            )

        file_predicates = set(self.relationship_types) | {p.value for p in self.functional_predicates}
        enum_predicates = {p.value for p in Predicate}
        if file_predicates != enum_predicates:
            problems.append(
                f"predicates mismatch: only-in-file={sorted(file_predicates - enum_predicates)}, "
                f"only-in-code={sorted(enum_predicates - file_predicates)}"
            )

        for spec in self.relationship_types.values():
            for label in [*spec.from_labels, *spec.to_labels]:
                if label != WILDCARD and label not in file_labels:
                    problems.append(f"{spec.name}: unknown endpoint label {label!r}")

        for name, node in self.node_types.items():
            if node.stored_as is not None and node.stored_as not in file_labels:
                problems.append(f"{name}: stored_as {node.stored_as!r} is not a known label")

        for enum_cls, values, key in (
            (ArtifactType, self.artifact_types, "artifact_types"),
            (ArtifactStatus, self.artifact_status, "artifact_status"),
            (EpisodeType, self.episode_types, "episode_types"),
            (Track, self.tracks, "tracks"),
        ):
            if {v.value for v in values} != {m.value for m in enum_cls}:
                problems.append(f"{key} does not match {enum_cls.__name__}")

        if problems:
            raise OntologyError(
                "Ontology does not match the domain contracts.", detail="; ".join(problems)
            )
        return self


def ontology_path() -> Path:
    """Default location of ``ontology.yaml`` (``/app/schemas`` in containers, repo otherwise)."""
    return default_schema_dir() / "ontology.yaml"


def _parse_node_types(raw: dict[str, Any]) -> dict[str, NodeTypeSpec]:
    out: dict[str, NodeTypeSpec] = {}
    for name, body in (raw or {}).items():
        body = body or {}
        out[name] = NodeTypeSpec(
            name=name,
            description=body.get("description", ""),
            functional=[Predicate(p) for p in body.get("functional", [])],
            aliases_seed=body.get("aliases_seed"),
            stored_as=body.get("stored_as"),
        )
    return out


def _parse_relationships(raw: dict[str, Any]) -> dict[str, RelationshipTypeSpec]:
    out: dict[str, RelationshipTypeSpec] = {}
    for group_name, members in (raw or {}).items():
        try:
            group = RelationshipGroup(group_name)
        except ValueError as exc:
            raise OntologyError(
                "Unknown relationship group.", detail=f"group={group_name!r}"
            ) from exc
        for name, body in (members or {}).items():
            body = body or {}
            out[name] = RelationshipTypeSpec(
                name=name,
                group=group,
                from_labels=list(body.get("from", [])),
                to_labels=list(body.get("to", [])),
            )
    return out


def _load_file(path: Path) -> Ontology:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OntologyError("Ontology file not found.", detail=str(path)) from exc
    except yaml.YAMLError as exc:
        raise OntologyError("Ontology file is not valid YAML.", detail=str(path)) from exc
    if not isinstance(raw, dict):
        raise OntologyError("Ontology file must be a mapping.", detail=str(path))

    try:
        ontology = Ontology(
            version=str(raw.get("version", "0.0.0")),
            node_types=_parse_node_types(raw.get("node_types", {})),
            relationship_types=_parse_relationships(raw.get("relationship_types", {})),
            functional_predicates=[Predicate(p) for p in raw.get("functional_predicates", [])],
            node_properties=PropertyContract(**(raw.get("node_properties") or {})),
            relationship_properties=PropertyContract(
                **(raw.get("relationship_properties") or {})
            ),
            artifact_types=[ArtifactType(v) for v in raw.get("artifact_types", [])],
            artifact_status=[ArtifactStatus(v) for v in raw.get("artifact_status", [])],
            episode_types=[EpisodeType(v) for v in raw.get("episode_types", [])],
            tracks=[Track(v) for v in raw.get("tracks", [])],
            source_path=path,
        )
    except OntologyError:
        raise
    except Exception as exc:  # ValueError from an enum, ValidationError from pydantic
        raise OntologyError("Ontology file could not be parsed.", detail=f"{path}: {exc}") from exc

    return ontology.validate_contracts()


@lru_cache(maxsize=4)
def _load_cached(path_str: str) -> Ontology:
    return _load_file(Path(path_str))


def load_ontology(path: Path | str | None = None, *, use_cache: bool = True) -> Ontology:
    """Load, validate and cache the ontology. ``path`` defaults to :func:`ontology_path`."""
    resolved = Path(path) if path is not None else ontology_path()
    if use_cache:
        return _load_cached(str(resolved))
    return _load_file(resolved)


def is_functional(predicate: Predicate | str, *, ontology: Ontology | None = None) -> bool:
    """Module-level shortcut used by the temporal rules: is this predicate single-valued?"""
    return (ontology or load_ontology()).is_functional(predicate)
