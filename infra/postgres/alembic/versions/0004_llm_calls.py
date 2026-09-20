"""0004_llm_calls - per-call LLM telemetry: tokens, latency, model, and the cost rate applied.

Revision ID: 0004_llm_calls
Revises: 0003_narrow_functional_index
Create Date: 2026-09-18

Owner: A04 (written by A00 during the observability request; ownership noted so it is not mistaken
for A04's own work).

Why this table exists
---------------------
The provider already measures everything needed and then throws it away.
`packages/aimemory/providers/llm/bedrock_provider.py` fills `prompt_tokens`, `completion_tokens`
and `duration_ms` on every completion, and nothing persists them. The consequence was visible
during the build: the owner asked what extraction had cost and the only answer available was the
AWS console, because the system itself had no idea. MEASURED afterwards by hand, ~190 documents
were paid for and 146 kept - a 23% waste rate that was invisible while it was happening.

One row per LLM call, not per episode. A single episode makes 2-3 calls (entity extraction, then
relationship extraction, plus an optional retry), and those calls have very different shapes: the
relationship call re-sends the document body and usually generates more output. Aggregating at the
episode level would hide that.

Cost
----
`rate_input_per_mtok` / `rate_output_per_mtok` are stored **on the row**, not looked up at read
time. Provider prices change, and a historical cost that silently re-prices itself when a rate is
edited is worse than useless for answering "what did last month actually cost". Storing the rate
that was applied keeps every past number reproducible and auditable. Both are nullable: a call
whose rate is unknown records its tokens honestly and leaves cost NULL rather than inventing a
figure.

`cost_usd` is a generated column so the arithmetic lives in one place and cannot drift between the
ops page, a report, and an ad-hoc query.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_llm_calls"
down_revision = "0003_narrow_functional_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # --- what ran -------------------------------------------------------------------------
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        #: Which step in the pipeline. `episode` = entity extraction, `relationship` = the second
        #: pass, `other` = anything else that starts calling the provider later.
        sa.Column("purpose", sa.Text(), nullable=False, server_default="other"),
        # --- what it belonged to (nullable: a call can happen outside a run, e.g. a benchmark) --
        sa.Column("episode_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        # --- what it consumed -------------------------------------------------------------------
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        #: Provider-side retries folded into this call (the provider retries invalid JSON itself).
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        # --- how it ended -----------------------------------------------------------------------
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("error", sa.Text(), nullable=True),
        # --- what it cost, at the rate in force when it ran --------------------------------------
        sa.Column("rate_input_per_mtok", sa.Numeric(12, 6), nullable=True),
        sa.Column("rate_output_per_mtok", sa.Numeric(12, 6), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('episode', 'relationship', 'other')", name="llm_calls_purpose_check"
        ),
        sa.CheckConstraint("attempts >= 1", name="llm_calls_attempts_check"),
    )

    # Cost is derived, never stored twice. NULL when either rate is unknown - an unpriced call
    # reports its tokens and abstains on cost rather than guessing.
    op.execute(
        """
        ALTER TABLE llm_calls ADD COLUMN cost_usd numeric(14, 8)
        GENERATED ALWAYS AS (
            (coalesce(prompt_tokens, 0)::numeric / 1000000) * rate_input_per_mtok
          + (coalesce(completion_tokens, 0)::numeric / 1000000) * rate_output_per_mtok
        ) STORED
        """
    )

    # The ops page asks three questions: what happened recently, what did this run cost, and how
    # does each model compare. One index each.
    op.create_index("ix_llm_calls_at", "llm_calls", [sa.text("at DESC")])
    op.create_index("ix_llm_calls_run", "llm_calls", ["run_id"], postgresql_where=sa.text("run_id IS NOT NULL"))
    op.create_index("ix_llm_calls_model", "llm_calls", ["model_id", sa.text("at DESC")])


def downgrade() -> None:
    op.drop_index("ix_llm_calls_model", table_name="llm_calls")
    op.drop_index("ix_llm_calls_run", table_name="llm_calls")
    op.drop_index("ix_llm_calls_at", table_name="llm_calls")
    op.drop_table("llm_calls")
