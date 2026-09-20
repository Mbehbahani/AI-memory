"""The ingestion worker's `metrics_snapshots` write, against the real database.

Why this file exists
--------------------
Every scan run from the Ops page after the Bedrock switch (ADR-0014) finished its work and then
reported itself **failed**, with::

    IntegrityError: (psycopg.errors.ForeignKeyViolation) insert or update on table
    "metrics_snapshots" violates foreign key constraint "metrics_snapshots_model_id_fkey"
    DETAIL: Key (model_id)=(qwen3:4b) is not present in table "extraction_models".

Two independent mistakes, one symptom:

* ``model_id`` was filled from ``settings.llm.model`` - the raw model name a person types into
  ``.env`` - while the column is a foreign key onto ``extraction_models.id``, a *registered* id. The
  two have never been the same string: the seeded Ollama row is ``qwen3-4b``, the setting is
  ``qwen3:4b``. The bug was therefore present before Bedrock; switching providers only guaranteed it
  fired every time.
* The write was unguarded, so bookkeeping *about* a finished scan could fail the scan. Nothing was
  lost - the ingest had already committed - but the Ops page said "failed" about work that had
  succeeded, which is the worst kind of wrong status.

These tests pin both halves. They assert behaviour, not a string: the first would still pass if the
provider changed again tomorrow.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

pytestmark = [pytest.mark.integration]


@pytest.fixture()
def scope(postgres_available: bool):
    """A minimal SessionScope over the real database - what the worker passes around."""
    from aimemory.common.config import get_settings
    from aimemory.persistence.db import Database

    database = Database(get_settings())

    class _Scope:
        def session(self):
            return database.session()

    return _Scope()


def test_snapshot_model_id_is_a_registered_extraction_model_not_the_raw_setting(scope: Any) -> None:
    from aimemory.common.config import get_settings
    from aimemory.sources.worker import _snapshot_model_id

    settings = get_settings()
    resolved = _snapshot_model_id(scope, settings)

    if resolved is None:
        pytest.skip("no extraction model registered in this database yet - nothing to verify")

    with scope.session() as session:
        known = session.execute(
            sa.text("SELECT 1 FROM extraction_models WHERE id = :id"), {"id": resolved}
        ).first()

    assert known is not None, (
        f"{resolved!r} is not in extraction_models, so the foreign key will reject it. This is the "
        f"exact failure that marked completed scans as failed."
    )


def test_writing_a_snapshot_does_not_violate_the_model_foreign_key(scope: Any) -> None:
    """The end-to-end reproduction: this call raised IntegrityError on every scan."""
    from aimemory.sources.worker import write_metrics_snapshot

    snapshot = write_metrics_snapshot(scope)

    assert snapshot is not None
    with scope.session() as session:
        row = session.execute(
            sa.text("SELECT model_id, scope FROM metrics_snapshots WHERE id = :id"),
            {"id": snapshot.id},
        ).one()
    assert row.scope == "ingestion_run"


def test_an_unregistered_model_records_null_rather_than_failing(scope: Any, monkeypatch) -> None:
    """A Tier-1-only scan never calls the LLM, so nothing has registered an identity yet.

    ``metrics_snapshots.model_id`` is nullable precisely for "this scope did not attribute to a
    model". Abstaining is correct; stamping an id the table does not hold is what broke.
    """
    from aimemory.sources import worker

    monkeypatch.setattr(
        "aimemory.sources.tier2.resolve_extraction_model_id",
        lambda *a, **k: "bedrock:a-model-that-was-never-registered",
    )
    from aimemory.common.config import get_settings

    assert worker._snapshot_model_id(scope, get_settings()) is None


def test_an_unresolvable_provider_does_not_raise(scope: Any, monkeypatch) -> None:
    """An unreachable or misconfigured provider must not take down a scan that already finished."""
    from aimemory.common.config import get_settings
    from aimemory.sources import worker

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("no credentials")

    monkeypatch.setattr("aimemory.sources.tier2.resolve_extraction_model_id", _boom)

    assert worker._snapshot_model_id(scope, get_settings()) is None
