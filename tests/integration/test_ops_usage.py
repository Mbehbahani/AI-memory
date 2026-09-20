"""P12-T02 follow-up (A16), integration: the Model usage section (``llm_calls``, migration 0004).

Two layers, both against a real PostgreSQL:

1. Query-level (``packages/aimemory/ops/queries.usage_view``) - seeded through ``pg_session``, whose
   outer transaction is always rolled back at teardown (``tests/conftest.py``), so nothing here is
   ever left behind in the real ``llm_calls`` table. A couple of tests briefly ``DELETE FROM
   llm_calls`` *inside that same transaction* to get a known-empty starting point before seeding -
   the delete never commits, so the live table (which A00's telemetry hook writes to) is untouched
   once the test ends.
2. HTTP-level (``/ops`` and ``/ops/parts/usage``) - renders against whatever ``llm_calls`` actually
   holds right now (may legitimately be empty), asserting the honest-zero language and the
   TTFT-is-not-measurable note are present either way.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from aimemory.ops import queries
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "apps" / "memory-api"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_available")]


def _insert_call(
    session: Session,
    *,
    provider: str = "bedrock",
    model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    purpose: str = "episode",
    episode_id: uuid.UUID | None = None,
    prompt_tokens: int | None = 1000,
    completion_tokens: int | None = 200,
    duration_ms: int | None = 2000,
    attempts: int = 1,
    ok: bool = True,
    error: str | None = None,
    rate_in: float | None = 1.0,
    rate_out: float | None = 5.0,
) -> None:
    session.execute(
        sa.text(
            """
            INSERT INTO llm_calls
                (id, provider, model_id, purpose, episode_id, prompt_tokens, completion_tokens,
                 duration_ms, attempts, ok, error, rate_input_per_mtok, rate_output_per_mtok)
            VALUES
                (:id, :provider, :model_id, :purpose, :episode_id, :prompt_tokens, :completion_tokens,
                 :duration_ms, :attempts, :ok, :error, :rate_in, :rate_out)
            """
        ),
        {
            "id": uuid.uuid4(),
            "provider": provider,
            "model_id": model_id,
            "purpose": purpose,
            "episode_id": episode_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "duration_ms": duration_ms,
            "attempts": attempts,
            "ok": ok,
            "error": error,
            "rate_in": rate_in,
            "rate_out": rate_out,
        },
    )


# ------------------------------------------------------------------------------------- query-level


def test_usage_view_on_an_empty_table_is_honest_zero_not_a_crash(pg_session: Session) -> None:
    pg_session.execute(sa.text("DELETE FROM llm_calls"))

    view = queries.usage_view(pg_session)

    assert view.has_calls is False
    assert view.total_calls == 0
    assert view.total_prompt_tokens == 0
    assert view.total_completion_tokens == 0
    assert view.total_cost_usd is None
    assert view.unpriced_calls == 0
    assert view.by_model == []
    assert view.by_purpose == []
    assert view.recent == []
    assert view.cost_per_document is None
    assert view.cost_per_fact is None
    assert view.error_rate_pct is None
    assert view.retry_rate_pct is None
    assert "Time-to-first-token is not measurable" in view.ttft_note


def test_usage_view_aggregates_cost_volume_speed_and_reliability(pg_session: Session) -> None:
    pg_session.execute(sa.text("DELETE FROM llm_calls"))
    episode = uuid.uuid4()

    # three "episode" calls, priced at the config/model-rates.yaml Haiku rate: (1000/1e6)*1 +
    # (200/1e6)*5 = 0.002 each -> 0.006 total.
    for duration in (1000, 2000, 3000):
        _insert_call(pg_session, purpose="episode", episode_id=episode, duration_ms=duration)

    # two "relationship" calls, same model, bigger prompt (re-sends the document body) - one of them
    # failed and needed a retry: (2000/1e6)*1 + (400/1e6)*5 = 0.004 each -> 0.008 total.
    _insert_call(
        pg_session,
        purpose="relationship",
        episode_id=episode,
        prompt_tokens=2000,
        completion_tokens=400,
        duration_ms=4000,
        attempts=1,
        ok=True,
    )
    _insert_call(
        pg_session,
        purpose="relationship",
        episode_id=episode,
        prompt_tokens=2000,
        completion_tokens=400,
        duration_ms=5000,
        attempts=2,
        ok=False,
        error="ValidationError: invalid JSON",
    )

    # a free local-model call (rate 0/0 - priced at zero, not unpriced).
    _insert_call(
        pg_session,
        provider="ollama",
        model_id="qwen3:4b",
        purpose="other",
        episode_id=episode,
        prompt_tokens=500,
        completion_tokens=100,
        duration_ms=6000,
        rate_in=0.0,
        rate_out=0.0,
    )

    # an unpriced call - no rate on file, cost_usd must stay NULL and be counted, not silently free.
    _insert_call(
        pg_session,
        provider="bedrock",
        model_id="some-unreleased-model",
        purpose="episode",
        episode_id=episode,
        prompt_tokens=300,
        completion_tokens=50,
        duration_ms=1500,
        rate_in=None,
        rate_out=None,
    )

    view = queries.usage_view(pg_session)

    assert view.has_calls is True
    assert view.total_calls == 7
    assert view.total_prompt_tokens == 1000 * 3 + 2000 * 2 + 500 + 300
    assert view.total_completion_tokens == 200 * 3 + 400 * 2 + 100 + 50
    assert view.unpriced_calls == 1
    assert view.total_cost_usd is not None
    assert view.total_cost_usd == pytest.approx(0.006 + 0.008 + 0.0, abs=1e-6)

    by_model = {row.model_id: row for row in view.by_model}
    assert by_model["us.anthropic.claude-haiku-4-5-20251001-v1:0"].calls == 5
    assert by_model["qwen3:4b"].calls == 1
    assert by_model["qwen3:4b"].cost_usd == pytest.approx(0.0)
    assert by_model["some-unreleased-model"].unpriced_calls == 1
    assert by_model["some-unreleased-model"].cost_usd is None

    by_purpose = {row.purpose: row for row in view.by_purpose}
    assert by_purpose["episode"].count == 4  # 3 priced + the 1 unpriced episode-purpose call
    assert by_purpose["episode"].median_ms == pytest.approx(1750.0)  # median of 1000,1500,2000,3000
    assert by_purpose["relationship"].count == 2
    assert by_purpose["relationship"].tokens_per_second == pytest.approx(800 / 9.0, rel=1e-3)

    # 1 of 7 failed, 1 of 7 needed a retry.
    assert view.error_rate_pct == pytest.approx(100.0 / 7, abs=0.1)
    assert view.retry_rate_pct == pytest.approx(100.0 / 7, abs=0.1)

    # cost tied to value: one document (the synthetic episode), zero facts extracted from it in this
    # transaction, so cost-per-document is a real number and cost-per-fact honestly abstains.
    assert view.documents_priced == 1
    assert view.cost_per_document == pytest.approx(view.total_cost_usd, abs=1e-6)
    assert view.facts_priced == 0
    assert view.cost_per_fact is None

    assert len(view.recent) == 7
    assert view.recent[0].at >= view.recent[-1].at  # newest first


def test_usage_view_cost_per_fact_uses_only_facts_from_the_same_episodes(pg_session: Session) -> None:
    """Ties the denominator to the numerator: cost-per-fact must reflect the facts extracted from
    exactly the episodes billed here, never the whole historical corpus (most of which predates
    ``llm_calls``, migration 0004)."""
    pg_session.execute(sa.text("DELETE FROM llm_calls"))

    row = pg_session.execute(
        sa.text(
            """
            SELECT episode_id, count(*) AS n
              FROM facts
             WHERE episode_id IS NOT NULL
             GROUP BY episode_id
             ORDER BY n DESC
             LIMIT 1
            """
        )
    ).first()
    if row is None:
        pytest.skip("no fact in the corpus carries an episode_id - nothing to tie cost to")
    episode_id, expected_fact_count = row

    _insert_call(pg_session, purpose="episode", episode_id=episode_id, prompt_tokens=1000, completion_tokens=200)

    view = queries.usage_view(pg_session)

    assert view.documents_priced == 1
    assert view.facts_priced == expected_fact_count
    assert view.cost_per_fact is not None
    # cost_per_fact is rounded to 5 decimal places by the query (queries.usage_view); the tolerance
    # here just absorbs that rounding, not a fuzzy assertion about the underlying arithmetic.
    assert view.cost_per_fact == pytest.approx(view.total_cost_usd / expected_fact_count, abs=1e-4)


# -------------------------------------------------------------------------------------- HTTP-level


@pytest.fixture(scope="module")
def client(postgres_available: bool):
    from app import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


def test_usage_section_is_on_the_full_page(client: Any) -> None:
    response = client.get("/ops")

    assert response.status_code == 200
    assert 'id="usage-section"' in response.text
    assert "Time-to-first-token is not measurable" in response.text


def test_usage_partial_renders_on_its_own_with_the_honest_zero_or_real_numbers(client: Any) -> None:
    body = client.get("/ops/parts/usage").text

    assert 'id="usage-section"' in body
    assert ("No calls recorded yet" in body) or ("total calls" in body)
    assert "cost per document" in body
    assert "cost per extracted fact" in body
    assert "unpriced call" in body


def test_usage_section_never_shows_a_traceback(client: Any) -> None:
    body = client.get("/ops/parts/usage").text

    assert "Traceback" not in body
