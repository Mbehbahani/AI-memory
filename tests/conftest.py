"""Shared pytest configuration for the whole suite (P2-T04, owner A12).

Every fixture here is designed around one rule: **the suite must be green with nothing running.**
Nobody should have to start Docker Desktop to get a clean ``pytest`` run of the unit tests, and
nobody should get a hard failure - just an honest, labelled skip - when a service happens to be down.

What lives here
----------------
* Session-cached **availability probes** (``postgres_available``, ``neo4j_available``,
  ``ollama_available``, ``embedding_available``, ``memory_api_available``, ``mcp_available``): each
  does one cheap reachability check (~2 s timeout), caches the result for the whole test session, and
  calls :func:`pytest.skip` with a clear reason the first (and only) time it is evaluated. A test that
  needs Postgres simply requests ``postgres_available`` (directly or via
  ``pytest.mark.usefixtures("postgres_available")``); it is auto-skipped, with a reason, when Postgres
  is not reachable - never a connection-refused traceback.
* **Connection fixtures** built on top of those probes and on :mod:`aimemory.common.config`:
  ``db_engine`` / ``pg_session`` (PostgreSQL, transaction-per-test, rolled back at teardown, and a
  skip - not an error - when the schema has not been migrated yet) and ``graph_store`` (Neo4j).
* ``tmp_source_root``: a disposable, mutable copy of ``tests/fixtures/mini-vault`` for ingestion and
  memory-scenario tests. It only ever copies the tiny, git-committed fixture into pytest's own
  ``tmp_path`` - it never reads or writes ``D:\\My-Vault`` or the pilot repo (CLAUDE.md, plan section T).
* ``fixed_now`` / ``uuid_factory``: deterministic values for temporal/provenance assertions, handed to
  constructors explicitly (see the docstring on ``fixed_now`` for why this project does not use a
  monkeypatched ambient clock).

Consumers: every test module. ``tests/unit/test_persistence_repositories.py`` (A04) is a documented
special case - see the note on ``pg_session`` below.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import sqlalchemy as sa
from aimemory.common.config import get_settings
from aimemory.persistence.db import register_pgvector
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

__all__ = [
    "SourceRootFixture",
]

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
MINI_VAULT_FIXTURE = FIXTURES_DIR / "mini-vault"

#: Never touch these. tmp_source_root only ever copies the committed mini-vault fixture (above) -
#: this constant exists solely so a future contributor sees, in one place, exactly what must never be
#: read or written by a test (CLAUDE.md, plan section T).
FORBIDDEN_SOURCE_ROOTS = (Path("D:/My-Vault"), Path("D:/AWS2/SupaBaseProject/DE"))

_AVAILABILITY_TIMEOUT = 2.0


# ====================================================================================================
# Availability probes
# ====================================================================================================


def _sanitize(message: str, secrets: list[str]) -> str:
    """Strip anything that might be a credential out of a probe failure message before it is ever
    shown in a skip reason (which pytest prints to the terminal and can end up in CI logs)."""
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    return message


@lru_cache(maxsize=1)
def _probe_postgres() -> tuple[bool, str]:
    settings = get_settings()
    try:
        engine = sa.create_engine(
            settings.postgres.dsn,
            future=True,
            connect_args={"connect_timeout": int(_AVAILABILITY_TIMEOUT)},
        )
        try:
            with engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
        finally:
            engine.dispose()
        return True, "reachable"
    except Exception as exc:  # noqa: BLE001 - any failure just means "not reachable"
        reason = _sanitize(
            f"{type(exc).__name__}: {exc}", [settings.postgres.password.get_secret_value()]
        )
        return False, reason


@lru_cache(maxsize=1)
def _probe_neo4j() -> tuple[bool, str]:
    settings = get_settings()
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            settings.neo4j.uri,
            auth=(settings.neo4j.user, settings.neo4j.password.get_secret_value()),
            connection_timeout=_AVAILABILITY_TIMEOUT,
        )
        try:
            driver.verify_connectivity()
        finally:
            driver.close()
        return True, "reachable"
    except Exception as exc:  # noqa: BLE001
        reason = _sanitize(
            f"{type(exc).__name__}: {exc}", [settings.neo4j.password.get_secret_value()]
        )
        return False, reason


def _probe_http(url: str) -> tuple[bool, str]:
    try:
        response = httpx.get(url, timeout=_AVAILABILITY_TIMEOUT)
        if response.status_code >= 500:
            return False, f"HTTP {response.status_code} from {url}"
        return True, "reachable"
    except httpx.HTTPError as exc:
        return False, f"{type(exc).__name__}: {exc}"


@lru_cache(maxsize=1)
def _probe_ollama() -> tuple[bool, str]:
    settings = get_settings()
    return _probe_http(f"{settings.llm.ollama_url}/api/tags")


@lru_cache(maxsize=1)
def _probe_embedding() -> tuple[bool, str]:
    settings = get_settings()
    return _probe_http(f"{settings.embedding.url}/health")


@lru_cache(maxsize=1)
def _probe_memory_api() -> tuple[bool, str]:
    settings = get_settings()
    return _probe_http(f"{settings.gateway.url}/health")


@lru_cache(maxsize=1)
def _probe_mcp() -> tuple[bool, str]:
    settings = get_settings()
    # McpSettings (packages/aimemory/common/config.py, frozen, owned by A02) has no base URL of its
    # own - only a host port. "mcp-server" is the compose service name, resolvable from inside the
    # `tools` container network; a host run (outside compose) will see this as unavailable, and so
    # will every run before apps/mcp-server exists (P10/P11). Both are correct, honest skips.
    url = f"http://mcp-server:{settings.mcp.host_port}/health"
    return _probe_http(url)


@pytest.fixture(scope="session")
def postgres_available() -> bool:
    ok, reason = _probe_postgres()
    if not ok:
        pytest.skip(f"PostgreSQL is not reachable ({reason}); start the compose stack or run tests "
                     "inside the `tools` container.")
    return True


@pytest.fixture(scope="session")
def neo4j_available() -> bool:
    ok, reason = _probe_neo4j()
    if not ok:
        pytest.skip(f"Neo4j is not reachable ({reason}); start the compose stack or run tests "
                     "inside the `tools` container.")
    return True


@pytest.fixture(scope="session")
def ollama_available() -> bool:
    ok, reason = _probe_ollama()
    if not ok:
        pytest.skip(f"Ollama is not reachable ({reason}); start the compose stack or run tests "
                     "inside the `tools` container.")
    return True


@pytest.fixture(scope="session")
def embedding_available() -> bool:
    ok, reason = _probe_embedding()
    if not ok:
        pytest.skip(f"embedding-service is not reachable ({reason}); start the compose stack or run "
                     "tests inside the `tools` container.")
    return True


@pytest.fixture(scope="session")
def memory_api_available() -> bool:
    ok, reason = _probe_memory_api()
    if not ok:
        pytest.skip(f"memory-api is not reachable ({reason}); it does not exist before P10/P11, or "
                     "the compose stack is not running.")
    return True


@pytest.fixture(scope="session")
def mcp_available() -> bool:
    ok, reason = _probe_mcp()
    if not ok:
        pytest.skip(f"mcp-server is not reachable ({reason}); it does not exist before P10/P11, or "
                     "the compose stack is not running.")
    return True


# ====================================================================================================
# PostgreSQL connection fixtures
# ====================================================================================================


@pytest.fixture(scope="session")
def db_engine(postgres_available: bool) -> Iterator[sa.Engine]:
    """A session-wide engine, only handed out once the schema is confirmed migrated.

    Skips (does not error) when PostgreSQL is reachable but ``alembic upgrade head`` has not been run
    yet - that is a setup gap, not a test failure.
    """
    settings = get_settings()
    engine = sa.create_engine(settings.postgres.dsn, future=True)
    register_pgvector(engine)
    try:
        with engine.connect() as conn:
            has_schema = conn.execute(sa.text("SELECT to_regclass('public.sources') IS NOT NULL")).scalar()
    except OperationalError as exc:  # pragma: no cover - postgres_available already checked reachability
        engine.dispose()
        pytest.skip(f"PostgreSQL query failed after a successful connectivity probe: {exc}")
    if not has_schema:
        engine.dispose()
        pytest.skip(
            "PostgreSQL schema is not migrated (table 'sources' is missing) - run "
            "`aimemory-ingest migrate` / `alembic upgrade head` first."
        )
    yield engine
    engine.dispose()


@pytest.fixture()
def pg_session(db_engine: sa.Engine) -> Iterator[Session]:
    """A session bound to a connection whose outer transaction is always rolled back at teardown.

    Mirrors ``tests/unit/test_persistence_repositories.py``'s own (pre-existing) fixture exactly,
    including tolerating a test that calls ``session.rollback()`` itself mid-test (the transaction is
    simply no longer active by the time teardown runs).
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


# ====================================================================================================
# Neo4j connection fixture
# ====================================================================================================


@pytest.fixture(scope="session")
def graph_store(neo4j_available: bool) -> Iterator[Any]:
    """A live :class:`~aimemory.persistence.graph_store.Neo4jGraphStore`, closed at session end.

    Tests are responsible for cleaning up whatever nodes/edges they create (by id) - this fixture
    never calls ``clear()``, which would wipe a shared dev graph (see ``tests/integration/test_graph_store.py``).
    """
    from aimemory.persistence.graph_store import Neo4jGraphStore

    settings = get_settings()
    store = Neo4jGraphStore(
        settings.neo4j.uri,
        settings.neo4j.user,
        settings.neo4j.password.get_secret_value(),
        database=settings.neo4j.database,
    )
    if not store.health():
        store.close()
        pytest.skip("Neo4j accepted a connection but health() reported unhealthy.")
    yield store
    store.close()


# ====================================================================================================
# tmp_source_root - a disposable, mutable copy of the mini-vault fixture
# ====================================================================================================


class SourceRootFixture:
    """A disposable, mutable copy of ``tests/fixtures/mini-vault`` for ingestion/scenario tests.

    Never touches a real source root - only ever mutates the copy living under pytest's own
    ``tmp_path``. Use the helpers below between two simulated ingestion runs to exercise the plan
    section L / Y change-detection cases (unchanged, modified, moved, deleted, duplicate, ...).
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, relative: str) -> Path:
        return self.root / relative

    def read(self, relative: str) -> str:
        return self.path(relative).read_text(encoding="utf-8")

    def write(self, relative: str, content: str) -> Path:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def modify(self, relative: str, content: str) -> Path:
        """Change the content (and therefore the hash) of an existing file - the ``modified`` case."""
        return self.write(relative, content)

    def touch(self, relative: str) -> None:
        """Update mtime only, content unchanged - exercises ``unchanged`` (hash-, not mtime-, based)."""
        path = self.path(relative)
        os.utime(path, None)

    def delete(self, relative: str) -> None:
        self.path(relative).unlink()

    def move(self, relative: str, new_relative: str) -> Path:
        target = self.path(new_relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(self.path(relative)), str(target))
        return target

    def duplicate(self, relative: str, new_relative: str) -> Path:
        target = self.path(new_relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.path(relative), target)
        return target


@pytest.fixture()
def tmp_source_root(tmp_path: Path) -> SourceRootFixture:
    dest = tmp_path / "mini-vault"
    shutil.copytree(MINI_VAULT_FIXTURE, dest)
    return SourceRootFixture(dest)


# ====================================================================================================
# Deterministic time / ids
# ====================================================================================================

#: A stable UTC instant, used the same way ``tests/unit/test_persistence_repositories.py``'s own
#: module-level ``NOW`` constant is used today.
FIXED_NOW: datetime = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def fixed_now() -> datetime:
    """A stable UTC instant for deterministic temporal/provenance assertions.

    This project does *not* monkeypatch ``aimemory.common.time.utc_now`` as an ambient clock: several
    domain models (``domain/models.py``) bind that function as a pydantic ``Field(default_factory=...)``
    at *import* time, so a monkeypatch applied by a fixture (which runs long after import) would never
    reach those already-bound factories, and would silently pass while doing nothing. Pass this value
    explicitly instead (``observed_at=fixed_now``, ...).
    """
    return FIXED_NOW


@pytest.fixture()
def uuid_factory() -> Callable[[str], UUID]:
    """Deterministic, reproducible UUIDs keyed by a human-readable label.

    ``uuid_factory("fact-a")`` returns the same UUID every time it is called with that label, within a
    test and across runs, so scenario/provenance assertions can refer to "the id of fact A" without
    threading a randomly generated ``uuid4()`` through several fixtures.
    """

    def _make(label: str) -> UUID:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:{label}")

    return _make
