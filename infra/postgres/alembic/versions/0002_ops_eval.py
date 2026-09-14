"""0002_ops_eval - ADR-0010/ADR-0011 tables: metrics_snapshots, extraction_reviews, run_requests,
service_stats (docs/architecture/data-model.md §5).

Revision ID: 0002_ops_eval
Revises: 0001_initial
Create Date: 2026-09-14

Owner: A04 (P5-T01, per the task brief promoting this split up from its originally planned P12/P14
slot so the ops/eval domain models have a table from the start of persistence work).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_ops_eval"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _exec(sql: str) -> None:
    op.execute(sa.text(sql))


METRICS_SCOPE = "('ingestion_run','eval','benchmark')"
REVIEW_VERDICT = "('accept','wrong','partial')"
RUN_ACTION = "('scan','retry_failed','eval','benchmark')"
RUN_REQUEST_STATUS = "('queued','running','done','failed','cancelled')"


def upgrade() -> None:
    _exec(
        f"""
        CREATE TABLE metrics_snapshots (
            id                          uuid PRIMARY KEY,
            at                          timestamptz NOT NULL DEFAULT now(),
            scope                       text NOT NULL CHECK (scope IN {METRICS_SCOPE}),
            run_id                      uuid NULL REFERENCES ingestion_runs(id),
            model_id                    text NULL REFERENCES extraction_models(id),
            schema_validity_rate       real,
            failed_episode_share       real,
            median_seconds_per_episode real,
            duplicate_entity_rate      real,
            unconfirmed_fact_share     real,
            coverage_by_project        jsonb NOT NULL DEFAULT '{{}}',
            gold_hit_at_5              real,
            expected_entity_presence   real,
            provenance_completeness    real,
            extra                      jsonb NOT NULL DEFAULT '{{}}'
        )
        """
    )
    _exec("CREATE INDEX ix_metrics_snapshots_at ON metrics_snapshots (scope, at DESC)")

    _exec(
        f"""
        CREATE TABLE extraction_reviews (
            id            uuid PRIMARY KEY,
            at            timestamptz NOT NULL DEFAULT now(),
            object_type   text NOT NULL,
            object_id     uuid NOT NULL,
            verdict       text NOT NULL CHECK (verdict IN {REVIEW_VERDICT}),
            reviewer      text NOT NULL DEFAULT 'owner',
            note          text,
            episode_id    uuid NULL REFERENCES episodes(id),
            model_id      text NULL REFERENCES extraction_models(id),
            sample_batch  text
        )
        """
    )
    _exec("CREATE INDEX ix_extraction_reviews_at ON extraction_reviews (at DESC)")
    _exec("CREATE INDEX ix_extraction_reviews_object ON extraction_reviews (object_type, object_id)")

    _exec(
        f"""
        CREATE TABLE run_requests (
            id            uuid PRIMARY KEY,
            action        text NOT NULL DEFAULT 'scan' CHECK (action IN {RUN_ACTION}),
            root_id       text NULL REFERENCES source_roots(root_id),
            tier          smallint NOT NULL DEFAULT 2 CHECK (tier IN (0,1,2)),
            options       jsonb NOT NULL DEFAULT '{{}}',
            requested_by  text NOT NULL DEFAULT 'ops-page',
            requested_at  timestamptz NOT NULL DEFAULT now(),
            status        text NOT NULL DEFAULT 'queued' CHECK (status IN {RUN_REQUEST_STATUS}),
            progress_pct  int NOT NULL DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
            message       text,
            started_at    timestamptz,
            finished_at   timestamptz,
            run_id        uuid NULL REFERENCES ingestion_runs(id),
            error         text
        )
        """
    )
    _exec(
        "CREATE INDEX ix_run_requests_queue ON run_requests (status, requested_at) "
        "WHERE status IN ('queued','running')"
    )

    _exec(
        """
        CREATE TABLE service_stats (
            id                 uuid PRIMARY KEY,
            at                 timestamptz NOT NULL DEFAULT now(),
            service            text NOT NULL,
            process_rss_bytes  bigint,
            model_loaded       boolean,
            model_name         text,
            details            jsonb NOT NULL DEFAULT '{}'
        )
        """
    )
    _exec("CREATE INDEX ix_service_stats_at ON service_stats (service, at DESC)")


def downgrade() -> None:
    for table in ("service_stats", "run_requests", "extraction_reviews", "metrics_snapshots"):
        _exec(f"DROP TABLE IF EXISTS {table} CASCADE")
