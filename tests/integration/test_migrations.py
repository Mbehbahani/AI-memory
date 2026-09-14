"""P5-T01 acceptance: `alembic upgrade head` -> `downgrade base` -> `upgrade head` clean on the
real PostgreSQL, and the resulting schema matches docs/architecture/data-model.md.

Marked `integration`: needs the compose network (run via `docker compose --profile tools run --rm
tools pytest tests/integration/test_migrations.py -q`, or against the loopback-published port from
the host). Destructive by design (`downgrade base` on the dev database, per CLAUDE.md's explicit
exception for exactly that command) - never run this against anything but the local dev database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from aimemory.common.config import get_settings
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "infra" / "postgres" / "alembic" / "alembic.ini"

EXPECTED_TABLES = {
    "devices", "projects", "project_aliases", "source_roots", "embedding_models", "extraction_models",
    "sources", "source_versions", "source_text", "mirror_blobs", "chunks", "embeddings",
    "episodes", "entities", "entity_mentions", "facts", "knowledge_artifacts", "artifact_entities",
    "ingestion_runs", "ingestion_jobs", "source_events", "retrieval_logs", "mcp_audit_log",
    "metrics_snapshots", "extraction_reviews", "run_requests", "service_stats",
}


@pytest.fixture(scope="module")
def engine() -> sa.Engine:
    eng = sa.create_engine(get_settings().postgres.dsn, future=True)
    try:
        with eng.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except OperationalError:
        eng.dispose()
        pytest.skip("PostgreSQL is not reachable from this test run.")
    return eng


def _alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def _table_names(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_upgrade_downgrade_upgrade_cycle_is_clean(engine: sa.Engine) -> None:
    cfg = _alembic_config()

    command.upgrade(cfg, "head")
    tables = _table_names(engine)
    assert EXPECTED_TABLES <= tables, f"missing: {EXPECTED_TABLES - tables}"

    command.downgrade(cfg, "base")
    after_downgrade = _table_names(engine) - {"alembic_version"}
    assert after_downgrade == set(), f"downgrade left tables behind: {after_downgrade}"

    command.upgrade(cfg, "head")
    tables_again = _table_names(engine)
    assert EXPECTED_TABLES <= tables_again, f"missing after re-upgrade: {EXPECTED_TABLES - tables_again}"


def test_seeds_are_present_and_idempotent(engine: sa.Engine) -> None:
    command.upgrade(_alembic_config(), "head")  # no-op if already at head
    with engine.connect() as conn:
        device = conn.execute(
            sa.text("SELECT id FROM devices WHERE id = 'local-development-machine'")
        ).first()
        assert device is not None

        minilm = conn.execute(
            sa.text("SELECT dimension, revision, normalized FROM embedding_models WHERE id = 'minilm-l6-v2-384'")
        ).first()
        assert minilm is not None
        assert minilm.dimension == 384
        assert minilm.normalized is True
        assert minilm.revision == "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"

        model_ids = {
            r.id for r in conn.execute(sa.text("SELECT id FROM extraction_models"))
        }
        assert {"deterministic:registry-v1", "qwen3-4b"} <= model_ids


def test_hnsw_index_exists_on_embeddings_vector(engine: sa.Engine) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'embeddings' "
                "AND indexname = 'ix_embeddings_vector'"
            )
        ).first()
        assert row is not None
        assert "hnsw" in row.indexdef
        assert "vector_cosine_ops" in row.indexdef


def test_provenance_view_exists_with_expected_columns(engine: sa.Engine) -> None:
    columns = {c["name"] for c in sa.inspect(engine).get_columns("provenance_v")}
    expected = {
        "object_type", "object_id", "project_id", "episode_id", "source_id", "source_uri",
        "source_hash", "source_version", "device_id", "observed_at", "valid_from", "valid_to",
        "confidence", "extraction_model_id", "embedding_model_id", "ingestion_run_id",
    }
    assert expected <= columns


def test_uq_facts_functional_current_partial_index_exists(engine: sa.Engine) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'facts' "
                "AND indexname = 'uq_facts_functional_current'"
            )
        ).first()
        assert row is not None
        assert "valid_to IS NULL" in row.indexdef
        for predicate in ("HAS_STATUS", "HAS_OWNER", "USES_ARCHITECTURE", "DEPLOYED_ON", "HAS_STAGE", "SELECTED_OPTION"):
            assert predicate in row.indexdef


def test_trigram_index_exists_on_entities_normalized_name(engine: sa.Engine) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'entities' "
                "AND indexname = 'ix_entities_trgm'"
            )
        ).first()
        assert row is not None
        assert "gin_trgm_ops" in row.indexdef


def test_chunks_tsv_is_a_generated_column(engine: sa.Engine) -> None:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT is_generated, generation_expression FROM information_schema.columns "
                "WHERE table_name = 'chunks' AND column_name = 'tsv'"
            )
        ).first()
        assert row is not None
        assert row.is_generated == "ALWAYS"
        assert "to_tsvector" in row.generation_expression
