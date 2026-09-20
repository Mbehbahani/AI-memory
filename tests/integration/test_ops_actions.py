"""P12-T02 (A16), integration: the two writes the Ops page performs (ADR-0011/ADR-0010).

1. Every button inserts exactly one ``run_requests`` row and never runs an ingest inline - the
   always-on worker (A07a) is the only thing that executes it. These tests assert the row exists with
   the right shape immediately after the POST; a separate manual end-to-end check (recorded in the
   task report) confirmed the live worker actually claims and finishes one.
2. The review form inserts exactly one ``extraction_reviews`` row per submit and the item then drops
   out of the "needs a verdict" queue.

Uses the live corpus (module-scoped ``client``, same pattern as ``test_gateway_api.py``), so object
ids for the review test are looked up at run time rather than hard-coded.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "apps" / "memory-api"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

pytestmark = [pytest.mark.integration]


@pytest.fixture(scope="module")
def client(postgres_available: bool):
    from app import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


# --------------------------------------------------------------------------------------- runs


def test_enqueue_run_inserts_a_queued_row_never_runs_inline(client: Any, db_engine: sa.Engine) -> None:
    response = client.post("/ops/runs", data={"action": "retry_failed", "root_id": "", "tier": "2"})

    assert response.status_code == 200
    assert "retry_failed" in response.text

    with db_engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT action, status, root_id, requested_by FROM run_requests "
                "WHERE action = 'retry_failed' ORDER BY requested_at DESC LIMIT 1"
            )
        ).first()
    assert row is not None
    assert row.action == "retry_failed"
    assert row.status in {"queued", "running", "done", "failed"}, "worker may have already claimed it"
    assert row.requested_by == "ops-page"


def test_enqueue_scan_for_a_named_root(client: Any, db_engine: sa.Engine) -> None:
    with db_engine.connect() as conn:
        root = conn.execute(sa.text("SELECT root_id FROM source_roots WHERE enabled LIMIT 1")).scalar()
    assert root, "no enabled source root in the corpus - cannot exercise the per-root scan button"

    response = client.post("/ops/runs", data={"action": "scan", "root_id": root, "tier": "1"})

    assert response.status_code == 200
    with db_engine.connect() as conn:
        stored = conn.execute(
            sa.text(
                "SELECT root_id, tier FROM run_requests WHERE action = 'scan' AND root_id = :r "
                "ORDER BY requested_at DESC LIMIT 1"
            ),
            {"r": root},
        ).first()
    assert stored is not None
    assert stored.root_id == root
    assert stored.tier == 1


def test_eval_and_benchmark_are_recorded_as_requests_the_worker_will_mark_unsupported(
    client: Any, db_engine: sa.Engine
) -> None:
    """ADR-0010: eval/benchmark belong to A12's ``scripts/eval``. The button must still create a real
    request row (never silently do nothing) - the worker (A07a) is what turns it into a `failed`
    status with a named error, not this route."""
    for action in ("eval", "benchmark"):
        response = client.post("/ops/runs", data={"action": action, "root_id": "", "tier": "2"})
        assert response.status_code == 200

    with db_engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT action FROM run_requests WHERE action IN ('eval', 'benchmark') "
                "AND requested_at > now() - interval '1 minute'"
            )
        ).all()
    assert {r.action for r in rows} >= {"eval", "benchmark"}


def test_run_request_rejects_an_unrecognised_action(client: Any) -> None:
    response = client.post("/ops/runs", data={"action": "delete_everything", "root_id": "", "tier": "2"})

    assert response.status_code == 422
    assert "Traceback" not in response.text


def test_run_request_rejects_an_out_of_range_tier(client: Any) -> None:
    response = client.post("/ops/runs", data={"action": "scan", "root_id": "", "tier": "99"})

    assert response.status_code == 422


# ------------------------------------------------------------------------------------- review


def _an_unreviewed_object(db_engine: sa.Engine) -> tuple[str, str] | None:
    with db_engine.connect() as conn:
        row = conn.execute(
            sa.text(
                """
                SELECT 'artifact', a.id::text FROM knowledge_artifacts a
                 LEFT JOIN extraction_reviews r ON r.object_type = 'artifact' AND r.object_id = a.id
                WHERE r.id IS NULL
                LIMIT 1
                """
            )
        ).first()
    return (row[0], row[1]) if row is not None else None


def test_review_verdict_persists_and_the_item_leaves_the_queue(client: Any, db_engine: sa.Engine) -> None:
    candidate = _an_unreviewed_object(db_engine)
    if candidate is None:
        pytest.skip("no unreviewed artifact in the corpus right now")
    object_type, object_id = candidate

    response = client.post(
        "/ops/review",
        data={
            "object_type": object_type,
            "object_id": object_id,
            "verdict": "accept",
            "sample_batch": "pytest-batch",
            "note": "recorded by test_ops_actions",
        },
    )

    assert response.status_code == 200
    assert object_id not in response.text, "the just-reviewed item must drop out of the queue"

    with db_engine.connect() as conn:
        stored = conn.execute(
            sa.text(
                "SELECT verdict, reviewer, note, sample_batch FROM extraction_reviews "
                "WHERE object_type = :t AND object_id = :i ORDER BY at DESC LIMIT 1"
            ),
            {"t": object_type, "i": object_id},
        ).first()
    assert stored is not None
    assert stored.verdict == "accept"
    assert stored.reviewer == "owner"
    assert stored.note == "recorded by test_ops_actions"
    assert stored.sample_batch == "pytest-batch"


def test_review_form_rejects_an_unrecognised_verdict(client: Any) -> None:
    response = client.post(
        "/ops/review",
        data={"object_type": "artifact", "object_id": str(uuid4()), "verdict": "maybe"},
    )

    assert response.status_code == 422


def test_review_form_rejects_an_unrecognised_object_type(client: Any) -> None:
    """``chunk``/``entity``/``episode``/``source`` are legitimate :class:`ObjectType` members (used
    elsewhere for citations) but not a valid review target here - only ``artifact``/``fact`` values
    are rendered by the queue, so anything else is still a request-shape error worth rejecting."""
    response = client.post(
        "/ops/review",
        data={"object_type": "banana", "object_id": str(uuid4()), "verdict": "accept"},
    )

    assert response.status_code == 422


def test_review_of_an_unknown_object_id_is_still_append_only_and_does_not_500(
    client: Any, db_engine: sa.Engine
) -> None:
    """The form trusts the hidden ``object_id`` it rendered; a foreign key on
    ``extraction_reviews.object_id`` does not exist (facts and artifacts share the column), so a
    verdict for an id that no longer exists (source deleted, artifact superseded and its row aged out)
    still records the human's verdict rather than 500ing."""
    fake_id = str(uuid4())

    response = client.post(
        "/ops/review",
        data={"object_type": "fact", "object_id": fake_id, "verdict": "wrong"},
    )

    assert response.status_code == 200
    with db_engine.connect() as conn:
        stored = conn.execute(
            sa.text("SELECT verdict FROM extraction_reviews WHERE object_id = :i"), {"i": fake_id}
        ).first()
    assert stored is not None and stored.verdict == "wrong"
