"""0003_narrow_functional_index - ADR-0015: narrow uq_facts_functional_current from the six
pre-ADR-0015 predicates to the three the ontology now calls functional.

Revision ID: 0003_narrow_functional_index
Revises: 0002_ops_eval
Create Date: 2026-09-17

Owner: A04. ADR-0015 (docs/adr/ADR-0015-predicate-cardinality-and-direction.md) narrowed
`functional_predicates` from `{HAS_STATUS, HAS_OWNER, USES_ARCHITECTURE, DEPLOYED_ON, HAS_STAGE,
SELECTED_OPTION}` to `{HAS_STATUS, HAS_STAGE, SELECTED_OPTION}`: `HAS_OWNER`, `USES_ARCHITECTURE`
and `DEPLOYED_ON` are ordinary multi-valued edges (co-ownership, multi-technology stacks and
multi-target deployment are not contradictions). Until this index matches, a second concurrently
true fact for one of the three demoted predicates raises `IntegrityError` at insert
(tests/integration/test_contracts_functional_index.py held this open as a strict xfail).

Narrowing a partial unique index can never fail on existing rows: the new predicate set is a
strict subset of the old one, so every pair the new index covers was already unique under the old
one. MEASURED against the live `aimemory` database before writing this migration (177 sources,
2,853 chunks, 614 entities, 1,244 facts, 729 artifacts, 147 episodes): zero (subject_entity_id,
predicate) pairs among the three kept predicates share more than one open (`valid_to IS NULL`) row.

`downgrade()` restores the six-predicate index. Because the new index is strictly narrower, the
downgrade can fail only if a row was inserted *after* the upgrade that duplicates
(subject_entity_id, predicate) for one of the three demoted predicates while both rows are still
open - exactly the case ADR-0015 says must now be legal. That is intended: a downgrade that would
silently drop one of those rows or refuse to run is the correct behaviour here, not a bug in this
migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_narrow_functional_index"
down_revision: str | None = "0002_ops_eval"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_PREDICATES = "('HAS_STATUS','HAS_OWNER','USES_ARCHITECTURE','DEPLOYED_ON','HAS_STAGE','SELECTED_OPTION')"
_NEW_PREDICATES = "('HAS_STATUS','HAS_STAGE','SELECTED_OPTION')"


def _exec(sql: str) -> None:
    op.execute(sa.text(sql))


def upgrade() -> None:
    _exec("DROP INDEX IF EXISTS uq_facts_functional_current")
    _exec(
        f"CREATE UNIQUE INDEX uq_facts_functional_current ON facts (subject_entity_id, predicate) "
        f"WHERE valid_to IS NULL AND predicate IN {_NEW_PREDICATES}"
    )


def downgrade() -> None:
    _exec("DROP INDEX IF EXISTS uq_facts_functional_current")
    _exec(
        f"CREATE UNIQUE INDEX uq_facts_functional_current ON facts (subject_entity_id, predicate) "
        f"WHERE valid_to IS NULL AND predicate IN {_OLD_PREDICATES}"
    )
