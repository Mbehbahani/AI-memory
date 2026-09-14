"""``aimemory-ingest migrate`` - applies every schema migration this project owns.

Owner: A04 (P5-T02). Run by the compose ``migrate`` one-shot service (``docker-compose.yml``)
before ``memory-api``/``ingestion``/``mcp-server`` start. Steps, all idempotent:

1. Alembic ``upgrade head`` against PostgreSQL (ADR-0001).
2. ``infra/neo4j/schema/constraints.cypher`` against Neo4j (``IF NOT EXISTS`` everywhere).
3. ``infra/neo4j/schema/readonly-user.cypher`` - creates the ``memory_reader`` credential. See that
   file for the documented Community Edition RBAC limitation: the user exists, but Neo4j Community
   cannot make it enforceably read-only (no roles). This is a recorded, accepted limitation, not a
   bug in this command.

This module is deliberately import-light and exposes a plain :func:`migrate` function in addition to
the Typer command, so ``cli/ingest.py`` (owned by A07a, P6) can mount it as a subcommand with
``ingest_app.add_typer(migrate_app)`` without this module needing to know that app exists yet.
"""

from __future__ import annotations

from pathlib import Path

import typer
from alembic import command
from alembic.config import Config
from neo4j import GraphDatabase

from ..common.config import get_settings
from ..persistence.graph_store import Neo4jGraphStore

__all__ = [
    "app",
    "apply_neo4j_constraints",
    "create_readonly_user",
    "migrate",
    "run_postgres_migrations",
]

app = typer.Typer(
    add_completion=False, help="Database and graph schema migrations (Alembic + Neo4j constraints)."
)

# packages/aimemory/cli/migrate.py -> parents[3] is the repository root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _REPO_ROOT / "infra" / "postgres" / "alembic" / "alembic.ini"
_CONSTRAINTS_CYPHER = _REPO_ROOT / "infra" / "neo4j" / "schema" / "constraints.cypher"
_READONLY_USER_CYPHER = _REPO_ROOT / "infra" / "neo4j" / "schema" / "readonly-user.cypher"


def _alembic_config() -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    return cfg


def run_postgres_migrations() -> None:
    """``alembic upgrade head``. Safe to call repeatedly: Alembic no-ops at the current head."""
    command.upgrade(_alembic_config(), "head")


def _statements(path: Path) -> list[str]:
    """One Cypher statement per non-blank, non-comment line (both schema files are written that
    way on purpose so no SQL/Cypher parser is needed here)."""
    statements: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        statements.append(stripped.rstrip(";"))
    return statements


def apply_neo4j_constraints() -> int:
    """Apply ``constraints.cypher``. Returns the number of statements applied."""
    settings = get_settings()
    statements = _statements(_CONSTRAINTS_CYPHER)
    with Neo4jGraphStore(
        settings.neo4j.uri,
        settings.neo4j.user,
        settings.neo4j.password.get_secret_value(),
        database=settings.neo4j.database,
    ) as store:
        store.apply_schema(statements)
    return len(statements)


def create_readonly_user() -> None:
    """Apply ``readonly-user.cypher`` against the ``system`` database (admin commands only run
    there). See that file for the Community Edition RBAC limitation this knowingly accepts."""
    settings = get_settings()
    statements = _statements(_READONLY_USER_CYPHER)
    driver = GraphDatabase.driver(
        settings.neo4j.uri, auth=(settings.neo4j.user, settings.neo4j.password.get_secret_value())
    )
    try:
        with driver.session(database="system") as session:
            for statement in statements:
                session.run(
                    statement,
                    readonly_user=settings.neo4j.readonly_user,
                    readonly_password=settings.neo4j.readonly_password.get_secret_value(),
                )
    finally:
        driver.close()


def migrate() -> None:
    """Run every step. Raises on the first failure - the caller (the Typer command, or a test)
    decides how to report it. Never logs a DSN or a password (``PostgresSettings.safe_dsn`` only)."""
    settings = get_settings()
    typer.echo(f"[migrate] postgres: {settings.postgres.safe_dsn}")
    run_postgres_migrations()
    typer.echo("[migrate] postgres migrations applied (alembic upgrade head)")

    typer.echo(f"[migrate] neo4j: {settings.neo4j.uri}")
    count = apply_neo4j_constraints()
    typer.echo(f"[migrate] applied {count} neo4j schema statements")

    create_readonly_user()
    typer.echo(
        f"[migrate] neo4j read-only user {settings.neo4j.readonly_user!r} ensured "
        "(Community Edition: no RBAC - see infra/neo4j/schema/readonly-user.cypher)"
    )
    typer.echo("[migrate] done")


@app.command("migrate")
def migrate_command() -> None:
    """Entry point wired to ``aimemory-ingest migrate`` (docker-compose.yml's ``migrate`` service)."""
    try:
        migrate()
    except Exception as exc:
        typer.echo(f"[migrate] FAILED: {exc}", err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app()
