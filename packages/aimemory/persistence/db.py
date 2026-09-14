"""SQLAlchemy engine/session plumbing for the repository layer (ADR-0001: Postgres is the system
of record). Synchronous, per the task brief ("keep them synchronous unless the spec says otherwise").

Consumers: every repository in :mod:`aimemory.persistence.repositories`, `cli/migrate.py`,
`tests/integration/test_migrations.py`, `tests/unit/test_persistence_*.py` (which may swap in a
``sessionmaker`` bound to a transaction that always rolls back).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from pgvector.psycopg import register_vector
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..common.config import Settings, get_settings

__all__ = ["Database", "get_database", "register_pgvector"]


def register_pgvector(engine: Engine) -> None:
    """Hook pgvector's psycopg3 adapter onto every connection this engine hands out, so a
    ``vector(384)`` column round-trips as a plain ``list[float]`` instead of a raw ``pgvector.Vector``
    (or, if this is skipped entirely, a bare text literal). Any engine talking to this schema's
    ``embeddings`` table - including test fixtures that build their own :func:`create_engine` rather
    than going through :class:`Database` - must call this once."""

    @event.listens_for(engine, "connect")
    def _register_vector_type(dbapi_connection: object, _record: object) -> None:
        register_vector(dbapi_connection)  # type: ignore[arg-type]


class Database:
    """Owns the engine and a session factory. One instance per process in normal operation."""

    def __init__(self, settings: Settings | None = None, *, echo: bool = False) -> None:
        self._settings = settings or get_settings()
        self.engine: Engine = create_engine(self._settings.postgres.dsn, echo=echo, future=True)
        register_pgvector(self.engine)

        self.session_factory: sessionmaker[Session] = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, future=True
        )

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A session that commits on clean exit and rolls back on exception."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()


@lru_cache(maxsize=1)
def get_database() -> Database:
    """Process-wide :class:`Database`. Tests call ``get_database.cache_clear()`` after overriding."""
    return Database()
