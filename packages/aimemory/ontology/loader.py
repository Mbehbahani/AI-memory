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
* ``functional`` lists on node types name predicates that are actually functional, and every
  functional predicate is claimed by at least one node type (that claim *is* its subject list -
  see :meth:`Ontology.functional_subjects`);
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
    "LITERAL",
    "WILDCARD",
    "FunctionalPredicateSpec",
    "NodeTypeSpec",
    "Ontology",
    "Orientation",
    "PropertyContract",
    "RelationshipTypeSpec",
    "is_functional",
    "load_ontology",
    "ontology_path",
]

#: ``"*"`` in a ``from``/``to`` list means "any label" (``MENTIONS``, ``RELATED_TO``, ...).
WILDCARD = "*"

#: ``literal`` in a functional predicate's ``to`` list means "the object is a value in
#: ``facts.object_value``, not a resolved entity" (``HAS_STATUS`` is the archetype). Lower-case so it
#: can never collide with a node label, all of which are ``CamelCase``.
LITERAL = "literal"


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


class FunctionalPredicateSpec(DomainModel):
    """One entry of ``functional_predicates`` (ADR-0015).

    Consumers: A08 (``apply_fact`` asks :meth:`Ontology.is_functional`; the projection asks
    :meth:`Ontology.validate_relationship` / :meth:`Ontology.orient`), A04 (the predicate list behind
    ``uq_facts_functional_current``), A12 (contract tests).

    A functional predicate is attribute-like, not an edge type, so it declares no ``from`` list: its
    legal subjects are the node types that name it in ``node_types.<T>.functional`` and are filled in
    at load time. ``reason`` records *why* one current value per subject is the truth for this
    predicate - the membership test ADR-0015 introduced after ``USES_ARCHITECTURE`` was found
    closing concurrently-true facts as ``historical``.
    """

    name: str
    subjects: list[str] = Field(default_factory=list)
    to_labels: list[str] = Field(default_factory=list)
    reason: str = ""

    @property
    def allows_literal(self) -> bool:
        """True when the object may be a value in ``facts.object_value`` rather than an entity."""
        return LITERAL in self.to_labels


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


class Orientation(DomainModel):
    """The result of :meth:`Ontology.orient`: which way round a triple must be written.

    Consumers: A08 (extraction persistence and the Neo4j projection call ``orient`` before writing a
    fact and record ``flipped`` so ``explain`` can show that the system, not the document, chose the
    direction), A12 (contract tests).

    ``flipped`` is ``True`` only when the triple as extracted is *illegal* in the declared direction
    and *legal* reversed - an unambiguous repair. When both directions are legal (``Person HAS_OWNER
    Person``) nothing is changed and ``flipped`` is ``False``; when neither is legal the call raises
    instead of guessing.
    """

    predicate: str
    from_label: str
    to_label: str
    flipped: bool = False
    reason: str = ""


class Ontology(DomainModel):
    """The parsed, validated ontology. Build it with :func:`load_ontology` (cached)."""

    version: str
    node_types: dict[str, NodeTypeSpec]
    relationship_types: dict[str, RelationshipTypeSpec]
    functional_predicates: list[Predicate]
    functional_specs: dict[str, FunctionalPredicateSpec] = Field(default_factory=dict)
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
        """ADR-0005 rule 1: does ``(subject, predicate)`` admit only one current object?

        ADR-0015 narrowed the answer: ``True`` only for predicates where two concurrent values are a
        contradiction (``HAS_STATUS``, ``HAS_STAGE``, ``SELECTED_OPTION``). ``USES_ARCHITECTURE``,
        ``HAS_OWNER`` and ``DEPLOYED_ON`` used to return ``True`` here and no longer do - a project
        legitimately uses several technologies at once.
        """
        name = predicate.value if isinstance(predicate, Predicate) else str(predicate)
        return name in {p.value for p in self.functional_predicates}

    def functional(self, predicate: Predicate | str) -> FunctionalPredicateSpec | None:
        """The functional spec, or ``None`` when the predicate is an edge type."""
        name = predicate.value if isinstance(predicate, Predicate) else str(predicate)
        return self.functional_specs.get(name)

    def functional_subjects(self, predicate: Predicate | str) -> list[str]:
        """Node types that may carry this functional predicate (their ``functional:`` lists)."""
        spec = self.functional(predicate)
        return list(spec.subjects) if spec else []

    def relationship(self, predicate: Predicate | str) -> RelationshipTypeSpec | None:
        """The edge spec, or ``None`` for a functional predicate (which is not an edge type)."""
        name = predicate.value if isinstance(predicate, Predicate) else str(predicate)
        return self.relationship_types.get(name)

    def group_of(self, predicate: Predicate | str) -> RelationshipGroup | None:
        spec = self.relationship(predicate)
        return spec.group if spec else None

    def validate_relationship(
        self, predicate: Predicate | str, from_label: str, to_label: str | None
    ) -> None:
        """Raise :class:`OntologyError` if this triple is not allowed in this direction.

        ``to_label=None`` means the object is a literal (``facts.object_value``) rather than a
        resolved entity; only a functional predicate that declares ``literal`` in its ``to`` list
        accepts one.

        Before ADR-0015 this method returned early for functional predicates ("endpoints are not
        constrained"), which is why ``Person -[HAS_OWNER]-> Project`` - the exact reverse of the
        declared direction - was written 38 times without a single complaint. Functional predicates
        now declare their subjects (via ``node_types.<T>.functional``) and their object kind, and are
        checked like every other predicate.
        """
        functional = self.functional(predicate)
        if functional is not None:
            self._validate_functional(functional, from_label, to_label)
            return

        spec = self.relationship(predicate)
        if spec is None:
            raise OntologyError("Unknown relationship type.", detail=f"predicate={predicate!r}")
        if to_label is None:
            raise OntologyError(
                "Only a functional predicate may take a literal object.",
                detail=f"{from_label} -[{spec.name}]-> <literal>",
            )
        # Both sides of the comparison must be expressed in the *same* vocabulary. The incoming
        # labels are mapped to storage, so the spec's endpoint lists - written conceptually in
        # `ontology.yaml` - have to be mapped the same way. Comparing a stored label against the raw
        # list made every type carrying a `stored_as` alias permanently unmatchable: `SubProject`
        # (stored_as: Project) became `Project`, which does not appear in PART_OF's
        # `from: [SubProject, Document, Task, Requirement]`, so `SubProject -[PART_OF]-> Project`
        # could never validate even though the ontology plainly intends to allow it.
        #
        # Normalising the lists means `Project -[PART_OF]-> Project` is now also accepted. That is
        # the unavoidable consequence of the `stored_as` design, not a widening of the rule: at the
        # graph level a sub-project *is* a `:Project` node, and what distinguishes it is `parent_id`,
        # not its label. A validator cannot tell the two apart, so it must accept both or reject both.
        if not self._allows_stored(spec, from_label, to_label):
            raise OntologyError(
                "Relationship endpoints violate the ontology.",
                detail=f"{from_label} -[{spec.name}]-> {to_label}",
            )

    def _validate_functional(
        self, spec: FunctionalPredicateSpec, from_label: str, to_label: str | None
    ) -> None:
        """Endpoint check for an attribute-like predicate (ADR-0015)."""
        allowed_from = self._stored_endpoints(spec.subjects)
        if self.stored_label(from_label) not in allowed_from:
            raise OntologyError(
                "Relationship endpoints violate the ontology.",
                detail=(
                    f"{from_label} -[{spec.name}]-> {to_label or '<literal>'}: "
                    f"{spec.name} may only describe {sorted(spec.subjects)}"
                ),
            )
        if to_label is None:
            if not spec.allows_literal:
                raise OntologyError(
                    "Relationship endpoints violate the ontology.",
                    detail=f"{from_label} -[{spec.name}]-> <literal>: object must be an entity",
                )
            return
        allowed_to = self._stored_endpoints([lbl for lbl in spec.to_labels if lbl != LITERAL])
        if WILDCARD in allowed_to or self.stored_label(to_label) in allowed_to:
            return
        raise OntologyError(
            "Relationship endpoints violate the ontology.",
            detail=(
                f"{from_label} -[{spec.name}]-> {to_label}: object must be "
                + ("a literal value" if spec.to_labels == [LITERAL] else f"one of {spec.to_labels}")
            ),
        )

    def orient(
        self, predicate: Predicate | str, from_label: str, to_label: str
    ) -> Orientation:
        """Return the triple in the direction the ontology declares, flipping it when unambiguous.

        Consumers: A08 (call this instead of :meth:`validate_relationship` when persisting an
        extracted fact whose object is a resolved entity).

        Three outcomes, and no fourth:

        * the triple is legal as written -> returned unchanged, ``flipped=False``;
        * it is illegal as written but legal reversed -> returned reversed, ``flipped=True``. This is
          the ``Mohammad HAS_OWNER JobLab`` case: a true statement said backwards, repaired instead
          of dropped;
        * it is illegal both ways, or legal both ways -> the first raises :class:`OntologyError`, the
          second returns it unchanged. Ambiguity is never resolved by guessing.

        Never call this with a literal object: a literal cannot become a subject, so there is nothing
        to flip. Use :meth:`validate_relationship` with ``to_label=None`` instead.
        """
        name = predicate.value if isinstance(predicate, Predicate) else str(predicate)
        forward = self._legal(name, from_label, to_label)
        if forward:
            return Orientation(
                predicate=name, from_label=from_label, to_label=to_label, flipped=False
            )
        if self._legal(name, to_label, from_label):
            return Orientation(
                predicate=name,
                from_label=to_label,
                to_label=from_label,
                flipped=True,
                reason=(
                    f"{from_label} -[{name}]-> {to_label} is not a declared direction; "
                    f"{to_label} -[{name}]-> {from_label} is"
                ),
            )
        self.validate_relationship(name, from_label, to_label)  # raises with the detailed message
        raise OntologyError(  # pragma: no cover - defensive; the line above always raises
            "Relationship endpoints violate the ontology.",
            detail=f"{from_label} -[{name}]-> {to_label}",
        )

    def _legal(self, predicate: str, from_label: str, to_label: str | None) -> bool:
        try:
            self.validate_relationship(predicate, from_label, to_label)
        except OntologyError:
            return False
        return True

    def _allows_stored(
        self, spec: RelationshipTypeSpec, from_label: str, to_label: str
    ) -> bool:
        """``spec.allows`` with both the endpoints *and* the spec's lists mapped to stored labels."""
        allowed_from = self._stored_endpoints(spec.from_labels)
        allowed_to = self._stored_endpoints(spec.to_labels)
        ok_from = WILDCARD in allowed_from or self.stored_label(from_label) in allowed_from
        ok_to = WILDCARD in allowed_to or self.stored_label(to_label) in allowed_to
        return ok_from and ok_to

    def _stored_endpoints(self, labels: list[str]) -> set[str]:
        """The endpoint names of a relationship spec, mapped through ``stored_as``.

        ``"*"`` is passed through untouched. A name that is not a known node type is kept verbatim
        rather than raising, so a typo in `ontology.yaml` still surfaces as a normal endpoint
        violation (and via :meth:`validate_contracts`) instead of as an unrelated lookup error.
        """
        out: set[str] = set()
        for name in labels:
            if name == WILDCARD:
                out.add(WILDCARD)
                continue
            try:
                out.add(self.stored_label(name))
            except OntologyError:
                out.add(name)
        return out

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

        overlap = set(self.relationship_types) & set(self.functional_specs)
        if overlap:
            problems.append(
                f"predicates declared both as edge types and as functional: {sorted(overlap)}"
            )

        for fname, fspec in self.functional_specs.items():
            for label in fspec.to_labels:
                if label not in (WILDCARD, LITERAL) and label not in file_labels:
                    problems.append(f"{fname}: unknown object label {label!r}")
            if not fspec.to_labels:
                problems.append(f"{fname}: no object kind declared (`to` is empty)")
            if not fspec.subjects:
                problems.append(
                    f"{fname}: no node type declares it in `functional:`, so it has no legal "
                    "subject and every fact using it would be rejected"
                )

        for name, node in self.node_types.items():
            if node.stored_as is not None and node.stored_as not in file_labels:
                problems.append(f"{name}: stored_as {node.stored_as!r} is not a known label")
            for predicate in node.functional:
                if predicate.value not in self.functional_specs:
                    problems.append(
                        f"{name}: `functional: [{predicate.value}]` but {predicate.value} is not a "
                        "functional predicate (ADR-0015 moved it to relationship_types)"
                    )

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


def _parse_functional(
    raw: Any, node_types: dict[str, NodeTypeSpec]
) -> dict[str, FunctionalPredicateSpec]:
    """Parse ``functional_predicates`` and fill each spec's subject list from the node types.

    Accepts the ADR-0015 mapping form (``{HAS_STATUS: {to: [...], reason: "..."}}``). The pre-0015
    list form is still read so an older copy of the file loads, but it yields specs with no declared
    object kind, which :meth:`Ontology.validate_contracts` then rejects - an old file fails loudly
    rather than silently reinstating the unchecked behaviour ADR-0015 removed.
    """
    subjects: dict[str, list[str]] = {}
    for label, node in node_types.items():
        for predicate in node.functional:
            subjects.setdefault(predicate.value, []).append(label)

    if isinstance(raw, list):
        entries: dict[str, dict[str, Any]] = {str(name): {} for name in raw}
    elif isinstance(raw, dict):
        entries = {str(name): (body or {}) for name, body in raw.items()}
    elif raw is None:
        entries = {}
    else:
        raise OntologyError(
            "functional_predicates must be a mapping.", detail=f"got {type(raw).__name__}"
        )

    out: dict[str, FunctionalPredicateSpec] = {}
    for name, body in entries.items():
        out[name] = FunctionalPredicateSpec(
            name=name,
            subjects=subjects.get(name, []),
            to_labels=[str(v) for v in body.get("to", [])],
            reason=" ".join(str(body.get("reason", "")).split()),
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
        node_types = _parse_node_types(raw.get("node_types", {}))
        functional_specs = _parse_functional(raw.get("functional_predicates"), node_types)
        ontology = Ontology(
            version=str(raw.get("version", "0.0.0")),
            node_types=node_types,
            relationship_types=_parse_relationships(raw.get("relationship_types", {})),
            functional_predicates=[Predicate(p) for p in functional_specs],
            functional_specs=functional_specs,
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
