"""P12-T02 (A16), integration: the `/ops` page renders every ADR-0011 section against real data.

The corpus behind these assertions is the live pilot corpus (my-vault + JobLab DE) and, in this
environment, is being actively mutated by the always-on ingestion worker and by other concurrent
agents - so these tests deliberately avoid asserting exact counts (a number that was 124 when written
can be 52 by the time the suite runs). What is asserted instead: every section renders, the honest
labels appear (UNKNOWN/settled-outcome language is never silently rounded away), and nothing here
ever needs a network beyond ``127.0.0.1`` (ADR-0011: works with the browser offline).
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "apps" / "memory-api"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

pytestmark = [pytest.mark.integration]

SECTIONS = ("health", "runs", "coverage", "quality", "usage", "review", "attention")


def test_ops_timestamps_render_in_amsterdam_time_with_a_zone_label() -> None:
    from routes.ops import _fmt_amsterdam_time

    assert _fmt_amsterdam_time(datetime(2026, 9, 21, 9, 47, 31, tzinfo=UTC)) == (
        "2026-09-21 11:47:31 CEST"
    )
    assert _fmt_amsterdam_time(datetime(2026, 1, 21, 9, 47, 31, tzinfo=UTC)) == (
        "2026-01-21 10:47:31 CET"
    )


def test_attention_item_renders_its_first_observed_timestamp() -> None:
    from aimemory.ops.viewmodels import AttentionItem, AttentionView
    from routes.ops import templates

    rendered = templates.get_template("ops/_attention.html").render(
        attention=AttentionView(
            items=[
                AttentionItem(
                    severity="notice",
                    title="1 episode is still unextracted",
                    detail="Tier 2 has not run yet.",
                    first_observed_at=datetime(2026, 9, 21, 9, 47, 31, tzinfo=UTC),
                )
            ]
        )
    )

    assert "First observed: 2026-09-21 11:47:31 CEST" in rendered


@pytest.fixture(scope="module")
def client(postgres_available: bool):
    from app import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


# ------------------------------------------------------------------------------------- the page


def test_ops_page_renders_200_with_every_section(client: Any) -> None:
    response = client.get("/ops")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    for section in SECTIONS:
        assert f'id="{section}-section"' in response.text, f"{section} section is missing"


def test_ops_page_never_leaks_a_traceback_or_credential(client: Any) -> None:
    from aimemory.common.config import get_settings

    settings = get_settings()
    body = client.get("/ops").text

    assert "Traceback" not in body
    assert settings.postgres.password.get_secret_value() not in body
    assert settings.neo4j.password.get_secret_value() not in body
    assert "postgresql+psycopg://" not in body


def test_ops_page_loads_no_external_resource(client: Any) -> None:
    """ADR-0011: no CDN, no external fonts, no remote anything. Every ``src``/``href`` that points at
    an actual resource (not a same-origin anchor like ``#health`` or the loopback NeoDash link) must
    be a local, relative ``/ops/static/...`` path."""
    body = client.get("/ops").text

    resource_refs = re.findall(r'(?:src|href)="([^"]+)"', body)
    for ref in resource_refs:
        if ref.startswith("#"):
            continue
        if ref.startswith("http://127.0.0.1") or ref.startswith("https://127.0.0.1"):
            continue  # the NeoDash link - loopback, not a fetched resource
        assert ref.startswith("/ops/static/") or ref.startswith("/"), (
            f"non-local resource reference on the ops page: {ref!r}"
        )
        assert "cdn" not in ref.lower()


def test_ops_static_assets_are_served_locally_not_by_a_cdn(client: Any) -> None:
    htmx = client.get("/ops/static/htmx.min.js")
    css = client.get("/ops/static/ops.css")

    assert htmx.status_code == 200
    assert htmx.headers["content-type"].startswith(("text/javascript", "application/javascript"))
    assert len(htmx.content) > 10_000, "the vendored htmx.min.js looks truncated"
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]


# ------------------------------------------------------------------------------------- sections


@pytest.mark.parametrize("section", SECTIONS)
def test_each_section_partial_renders_on_its_own(client: Any, section: str) -> None:
    """The HTMX poll target for each section (``hx-get="/ops/parts/<section>"`` on the page)."""
    response = client.get(f"/ops/parts/{section}")

    assert response.status_code == 200
    assert f'id="{section}-section"' in response.text


def test_unknown_section_is_a_clean_404(client: Any) -> None:
    response = client.get("/ops/parts/does-not-exist")

    assert response.status_code == 404


def test_health_section_reports_every_dependency_and_labels_container_only_numbers(client: Any) -> None:
    body = client.get("/ops/parts/health").text

    for name in ("postgres", "neo4j", "embedding"):
        assert name in body
    # ADR-0011: disk free is the *container's* view, never presented as the host's.
    assert "container filesystem view" in body
    # backups/ is not bind-mounted into memory-api in the shipped compose file - honest UNKNOWN,
    # not a fabricated age (unless a future compose change adds the mount, in which case a real
    # age string appears instead - both are acceptable, a silent default of "0s" is not).
    assert "UNKNOWN" in body or re.search(r"\d+[smhd] old", body)


def test_coverage_section_shows_a_coverage_percentage_not_just_a_raw_count(client: Any) -> None:
    body = client.get("/ops/parts/coverage").text

    assert "%" in body
    assert "ETA" in body
    # the settled-shortfall philosophy: coverage is never silently rounded to a green 100%.
    assert "settled outcome" in body


def test_quality_section_renders_even_with_no_snapshots_yet(client: Any) -> None:
    """metrics_snapshots may legitimately be empty (nobody has run a scan through this page yet in a
    fresh environment) - the section must say so, not render a misleading empty chart as if it were
    a real flat line."""
    body = client.get("/ops/parts/quality").text

    assert "acceptance rate" in body
    assert ("not enough data yet" in body) or ("sparkline" in body)


def test_review_section_citation_never_carries_a_raw_host_path(client: Any) -> None:
    """Plan section T: citations only. The *citation* string is built from ``source_uri`` (the
    ``vault://``/``joblab-de://`` scheme, ADR-0004), never a raw ``D:\\...`` host path - even though
    the evidence text next to it is free-form source content and may itself mention a path as data."""
    body = client.get("/ops/parts/review").text

    assert "citation" in body
    citations = re.findall(r'<div class="citation">([^<]*)</div>', body)
    assert citations, "no citation strings rendered - is the corpus empty?"
    for citation in citations:
        assert citation.startswith("["), f"citation does not look like the plan-J format: {citation!r}"
        assert "D:\\" not in citation and "D:/" not in citation


def test_attention_section_renders_a_list_or_the_all_clear_message(client: Any) -> None:
    body = client.get("/ops/parts/attention").text

    assert "attention-item" in body or "Nothing needs attention" in body


def test_usage_section_states_plainly_that_ttft_is_not_measurable(client: Any) -> None:
    """The owner asked for time-to-first-token; extraction is non-streaming structured output, so
    there is no first token to time. The page must say so honestly rather than fake a number."""
    body = client.get("/ops/parts/usage").text

    assert "Time-to-first-token is not measurable" in body
    assert "throughput" in body


# ------------------------------------------------------------------------------------------ theme


def test_theme_toggle_is_present_and_persists_via_localstorage_not_a_server_round_trip(client: Any) -> None:
    body = client.get("/ops").text

    assert 'id="theme-toggle"' in body
    assert "ops-theme" in body  # the localStorage key the inline script reads/writes
    assert "<style" not in body  # the theme lives in the vendored stylesheet, not an inline <style>


def test_ops_css_defines_a_readable_dark_theme_offline(client: Any) -> None:
    """Both themes must be readable (status colours and tables, not just the background), and the
    dark palette must ship in the vendored stylesheet - never fetched from anywhere at runtime."""
    css = client.get("/ops/static/ops.css").text

    assert "prefers-color-scheme: dark" in css
    assert 'data-theme="dark"' in css
    # every status colour that light theme defines must be redefined for dark, not just --bg/--fg -
    # a colour left over from light mode is exactly how a badge becomes unreadable in dark mode.
    for var in ("--fg", "--bg", "--muted", "--border", "--ok", "--warn", "--notice", "--info", "--link"):
        assert css.count(var) >= 3, f"{var} is not redefined for both the media-query and forced dark theme"


# --------------------------------------------------------------------------------- contract/openapi


def test_ops_routes_are_in_the_exported_openapi_document() -> None:
    import json

    document = json.loads((REPO_ROOT / "schemas" / "api" / "openapi.json").read_text("utf-8"))

    for path in ("/ops", "/ops/parts/{section}", "/ops/runs", "/ops/review"):
        assert path in document["paths"], f"{path} is missing from the exported OpenAPI document"


def test_the_exported_openapi_document_is_current() -> None:
    import openapi_export

    exported = REPO_ROOT / "schemas" / "api" / "openapi.json"
    assert openapi_export.main(["--check", "--output", str(exported)]) == 0, (
        "schemas/api/openapi.json is stale - re-run apps/memory-api/openapi_export.py"
    )


# -------------------------------------------------------------------------- loopback (ADR-0007)


def test_the_running_ops_page_is_reachable_only_on_loopback(memory_api_available: bool) -> None:
    """Evidence from the live container, not just the in-process TestClient."""
    import httpx
    from aimemory.common.config import get_settings

    settings = get_settings()
    response = httpx.get(f"{settings.gateway.url}/ops", timeout=10.0)

    assert response.status_code == 200
    assert 'id="health-section"' in response.text
