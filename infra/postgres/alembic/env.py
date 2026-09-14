"""Alembic runtime environment.

Consumers: `aimemory-ingest migrate` (packages/aimemory/cli/migrate.py) and direct
`alembic -c infra/postgres/alembic/alembic.ini upgrade head` invocations (A04 verification).

The DSN is never read from alembic.ini: it comes from `aimemory.common.config.get_settings()`,
the same typed settings object every other process uses, so the password lives only in `.env`.
There is no SQLAlchemy ORM metadata to autogenerate against - every migration in this project is
hand-written raw DDL (see `docs/architecture/data-model.md`), so `target_metadata` stays `None` and
`alembic revision --autogenerate` is not part of this project's workflow.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# infra/postgres/alembic/env.py -> parents[3] is the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PACKAGES = _REPO_ROOT / "packages"
if str(_PACKAGES) not in sys.path:
    sys.path.insert(0, str(_PACKAGES))

from aimemory.common.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    """The real DSN, from settings, never logged (``PostgresSettings.dsn``, not ``safe_dsn``)."""
    return get_settings().postgres.dsn


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (``alembic upgrade head --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection - the normal path for `aimemory-ingest migrate`."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
