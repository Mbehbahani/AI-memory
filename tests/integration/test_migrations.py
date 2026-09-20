"""P5-T01 acceptance: `alembic upgrade head` -> `downgrade base` -> `upgrade head` clean, and the
resulting schema matches docs/architecture/data-model.md.

Marked `integration`: needs the compose network (run via `docker compose --profile tools run --rm
tools pytest tests/integration/test_migrations.py -q`, or against the loopback-published port from
the host).

Runs against a **throwaway database** (``scratch_dsn`` below), not the shared dev database. This
module used to run `alembic downgrade base` directly against `get_settings().postgres.dsn` - CLAUDE.md
carves out an explicit exception for that exact command, so the test was never *wrong*, but once a
real corpus existed in that database (176 sources / 2,838 chunks / 2,779 embeddings from a ~2.5 minute
`D:\\My-Vault` ingest) two agents independently lost it to a plain `pytest` run: `downgrade base` drops
every table (confirmed by the `sources` table's OID changing across the run while
`pg_stat_user_tables.n_tup_del` stayed 0 - a DROP + CREATE, not a DELETE). A12 fixed this by pointing
the whole cycle at a database created fresh for this module and dropped at teardown, on the same
PostgreSQL server (the `aimemory` role has CREATEDB) - so the acceptance is still genuinely exercised,
still runs by default with the rest of the suite, and a plain `docker compose --profile tools run --rm
tools pytest -q` now leaves any existing corpus in the real `aimemory` database completely untouched.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from aimemory.common.config import get_settings
from aimemory.ontology import load_ontology
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "infra" / "postgres" / "alembic" / "alembic.ini"
SCRATCH_DB_PREFIX = "aimemory_test_migrations_scratch_"

EXPECTED_TABLES = {
    "devices", "projects", "project_aliases", "source_roots", "embedding_models", "extraction_models",
    "sources", "source_versions", "source_text", "mirror_blobs", "chunks", "embeddings",
    "episodes", "entities", "entity_mentions", "facts", "knowledge_artifacts", "artifact_entities",
    "ingestion_runs", "ingestion_jobs", "source_events", "retrieval_logs", "mcp_audit_log",
    "metrics_snapshots", "extraction_reviews", "run_requests", "service_stats",
}


@contextmanager
def _database_url_pointing_at(dsn: str) -> Iterator[None]:
    """Point `aimemory.common.config.get_settings()` - and so `infra/postgres/alembic/env.py`, which
    reads its DSN from there, never from `alembic.ini` - at ``dsn`` for the duration of the `with`
    block, then restore whatever `DATABASE_URL` was before. Pytest runs this module's tests serially
    (no xdist in this project), so the window where the process-wide setting is not the real one is
    confined to a single alembic command invocation.
    """
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    get_settings.cache_clear()
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original
        get_settings.cache_clear()


@pytest.fixture(scope="module")
def scratch_dsn() -> Iterator[str]:
    """A brand-new, empty database on the same PostgreSQL server as the real `aimemory` database,
    created before this module's tests and dropped after - see the module docstring for why."""
    try:
        base_url = make_url(get_settings().postgres.dsn)
        admin_engine = sa.create_engine(
            base_url.set(database="postgres"), future=True, isolation_level="AUTOCOMMIT"
        )
        with admin_engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL is not reachable from this test run: {exc}")

    scratch_name = f"{SCRATCH_DB_PREFIX}{uuid.uuid4().hex[:8]}"
    try:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{scratch_name}"'))
    except OperationalError as exc:
        admin_engine.dispose()
        pytest.skip(
            f"could not create a scratch database for the migration-cycle test (needs CREATEDB "
            f"on the 'aimemory' role): {exc}"
        )

    scratch_url = base_url.set(database=scratch_name)
    scratch_engine = sa.create_engine(scratch_url, future=True, isolation_level="AUTOCOMMIT")
    with scratch_engine.connect() as conn:
        # The extensions the migrations assume exist (normally created once, for the real `aimemory`
        # database only, by infra/postgres/init/01-extensions.sql on first container start).
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    scratch_engine.dispose()

    try:
        # `str(URL)` masks the password as `***` - rendering it that way here would hand every
        # consumer of this fixture (and `DATABASE_URL`, via `_database_url_pointing_at`) a DSN that
        # fails authentication.
        yield scratch_url.render_as_string(hide_password=False)
    finally:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{scratch_name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture(scope="module")
def engine(scratch_dsn: str) -> Iterator[sa.Engine]:
    eng = sa.create_engine(scratch_dsn, future=True)
    yield eng
    eng.dispose()


def _alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def _table_names(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def test_upgrade_downgrade_upgrade_cycle_is_clean(scratch_dsn: str, engine: sa.Engine) -> None:
    cfg = _alembic_config()

    with _database_url_pointing_at(scratch_dsn):
        command.upgrade(cfg, "head")
    tables = _table_names(engine)
    assert EXPECTED_TABLES <= tables, f"missing: {EXPECTED_TABLES - tables}"

    with _database_url_pointing_at(scratch_dsn):
        command.downgrade(cfg, "base")
    after_downgrade = _table_names(engine) - {"alembic_version"}
    assert after_downgrade == set(), f"downgrade left tables behind: {after_downgrade}"

    with _database_url_pointing_at(scratch_dsn):
        command.upgrade(cfg, "head")
    tables_again = _table_names(engine)
    assert EXPECTED_TABLES <= tables_again, f"missing after re-upgrade: {EXPECTED_TABLES - tables_again}"


def test_seeds_are_present_and_idempotent(scratch_dsn: str, engine: sa.Engine) -> None:
    with _database_url_pointing_at(scratch_dsn):
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
    """The index exists after a full migration cycle and covers exactly the functional predicates.

    The predicate list is read from `schemas/ontology.yaml` rather than hard-coded: it used to name
    the six pre-ADR-0015 predicates, and revision `0003_narrow_functional_index` narrowed it to
    three. Deriving it means the next cardinality change breaks the migration or the ontology - the
    two places that should break - instead of this assertion.
    """
    functional = {p.value for p in load_ontology().functional_predicates}
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'facts' "
                "AND indexname = 'uq_facts_functional_current'"
            )
        ).first()
        assert row is not None
        assert "valid_to IS NULL" in row.indexdef
        for predicate in sorted(functional):
            assert predicate in row.indexdef
        for demoted in ("HAS_OWNER", "USES_ARCHITECTURE", "DEPLOYED_ON"):
            assert demoted not in row.indexdef, f"ADR-0015 demoted {demoted}; 0003 must not cover it"


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
