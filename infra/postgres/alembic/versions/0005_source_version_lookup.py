"""0005_source_version_lookup - index the "newest version at or before T" lookup retrieval now does.

Revision ID: 0005_source_version_lookup
Revises: 0004_llm_calls
Create Date: 2026-09-19

Owner: A04 (written by A00 while fixing the retrieval version leak; ownership noted so it is not
mistaken for A04's own work).

Why
---
Retrieval previously scoped chunks by project, source status, policy, secret flag and ``since`` - and
never by version, so every version of a file that had ever been indexed stayed searchable forever,
each with its own embeddings. ``retrieval/filters.py::CURRENT_VERSION_PREDICATE`` closes that by
selecting, per source, the newest version observed at or before the query's ``as_of``:

    AND NOT EXISTS (SELECT 1 FROM source_versions sv2
                     WHERE sv2.source_id = sv.source_id
                       AND sv2.observed_at <= :as_of
                       AND (sv2.observed_at, sv2.id) > (sv.observed_at, sv.id))

Migration 0001 indexes ``(source_id, is_current) WHERE is_current`` and ``(observed_at DESC)``.
Neither serves that correlated subquery: the first cannot answer a historical ``as_of`` at all, and
the second is not scoped by source, so the planner has to filter every version ever recorded for each
candidate row. This index gives the subquery an exact match - seek to the source, walk descending,
stop at the first row satisfying the timestamp bound.

The column order matches the comparison order in the predicate, ``id`` included, so the tie-break
that guarantees exactly one winner is served by the index rather than by a sort.

Index only. No table, column or data is touched, so ``downgrade`` is a clean drop and neither
direction can lose anything.
"""

from __future__ import annotations

from alembic import op

revision = "0005_source_version_lookup"
down_revision = "0004_llm_calls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_source_versions_source_observed
            ON source_versions (source_id, observed_at DESC, id DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_source_versions_source_observed")
