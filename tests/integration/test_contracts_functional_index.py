"""The ontology's functional predicate list and the database backstop must name the same predicates.

Owner: A02 (contract). Consumers of the guarantee: A04 (owns the migration that creates the index),
A08 (``apply_fact`` inserts a second open fact for a *non*-functional predicate and would hit the
index if the two lists ever disagreed).

``uq_facts_functional_current`` is a partial unique index on ``(subject_entity_id, predicate) WHERE
valid_to IS NULL AND predicate IN (...)``. It is the database's half of ADR-0005 rule 1: at most one
open fact per subject for a single-valued predicate. ADR-0015 narrowed that set from six predicates
to three (``HAS_OWNER``, ``USES_ARCHITECTURE`` and ``DEPLOYED_ON`` are multi-valued and moved to
``relationship_types.semantic``). Migration ``0003_narrow_functional_index`` (A04) narrows the index
to match, so a second concurrently-true ``USES_ARCHITECTURE`` fact for the same subject no longer
raises ``IntegrityError`` at insert time.
"""

from __future__ import annotations

import re

import pytest
import sqlalchemy as sa
from aimemory.domain.enums import Predicate
from aimemory.ontology import load_ontology

pytestmark = pytest.mark.integration

#: Quoted string literals in the index's WHERE clause, e.g. ``'HAS_STATUS'::text``.
_QUOTED = re.compile(r"'([^']+)'")


def _index_predicates(engine: sa.Engine) -> set[str]:
    """The predicate names the live ``uq_facts_functional_current`` index actually covers.

    Only quoted literals that are real :class:`Predicate` members count. An earlier version of this
    helper kept every upper-case token in ``indexdef``, which silently swept up ``WHERE``, ``UNIQUE``,
    ``ANY``, ``NULL`` and friends - harmless for the subset assertion below, but it made the equality
    assertion compare a set of SQL keywords against a set of predicates and fail for the wrong reason.
    """
    with engine.connect() as conn:
        definition = conn.execute(
            sa.text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'facts' "
                "AND indexname = 'uq_facts_functional_current'"
            )
        ).scalar()
    assert definition, "uq_facts_functional_current is missing entirely"
    known = {p.value for p in Predicate}
    found = {name for name in _QUOTED.findall(definition) if name in known}
    assert found, f"no predicate literal found in the index definition: {definition!r}"
    return found


def test_database_backstop_covers_exactly_the_functional_predicates(db_engine: sa.Engine) -> None:
    """The two lists must name the same predicates, in both directions.

    Covering *more* than the ontology turns a legitimate second current fact into an
    ``IntegrityError``; covering *less* removes the backstop silently.
    """
    ontology = {p.value for p in load_ontology().functional_predicates}
    assert _index_predicates(db_engine) == ontology


def test_database_backstop_is_not_narrower_than_the_ontology(db_engine: sa.Engine) -> None:
    """The half that must hold even mid-migration: every functional predicate is backstopped.

    A predicate the ontology calls single-valued but the index does not cover would let two open
    facts coexist with no error at all - the failure mode ADR-0005 rule 1 exists to prevent. Kept as
    a separate assertion from the equality above so that, if the two lists ever drift again, the
    failure message says which direction they drifted in.
    """
    ontology = {p.value for p in load_ontology().functional_predicates}
    assert ontology <= _index_predicates(db_engine)


def test_the_demoted_predicates_are_no_longer_backstopped(db_engine: sa.Engine) -> None:
    """ADR-0015's operational payoff: these three may now hold several open facts per subject.

    This is the assertion that would have caught the original defect. While they were covered by the
    index, a project could only ever have one current ``USES_ARCHITECTURE`` value, so the temporal
    engine had to close a concurrently-true fact as ``historical`` to insert the next one.
    """
    covered = _index_predicates(db_engine)
    for predicate in (Predicate.HAS_OWNER, Predicate.USES_ARCHITECTURE, Predicate.DEPLOYED_ON):
        assert predicate.value not in covered, (
            f"{predicate.value} is multi-valued under ADR-0015 but the database still enforces "
            "one open fact per subject for it"
        )
