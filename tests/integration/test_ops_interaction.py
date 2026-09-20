"""P12-T02 (A16), integration: the `/ops` page has to be *usable*, not merely correct.

Three defects were reported from real use of the dashboard, and all three are properties of the
markup rather than of the data behind it, so they are testable here:

1. **The controls reset themselves.** The whole Runs section polled ``every 4s`` and HTMX replaces a
   polled element's entire ``innerHTML``. The root ``<select>`` was therefore rebuilt under the
   pointer roughly every four seconds and a choice could not be held long enough to submit. The fix
   splits the section; the invariant that keeps it fixed is *a polled partial contains no form
   control*, which is what these tests assert.
2. **One error filled the screen.** A ``ForeignKeyViolation`` arrives as ~2,000 characters on a
   single unbroken line and pushed the six columns that matter out of view.
3. **No way to tell what a section was for**, and no way to fold one away.

These assert on rendered HTML deliberately. The defects were invisible to every existing test because
each one checks that the *data* is present - and the data always was.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "apps" / "memory-api"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

pytestmark = [pytest.mark.integration]

#: Every partial the page re-fetches on a timer. Anything listed here gets its whole DOM thrown away
#: and rebuilt, so none of them may hold something the operator can be part-way through using.
POLLED_PARTS = ("health", "runs-activity", "coverage", "quality", "usage", "attention")

#: Rendered once by the full page and never swapped - these are allowed to hold controls.
STATIC_PARTS = ("runs", "review")

_CONTROL = re.compile(r"<(select|textarea)\b|<input\b(?![^>]*type=[\"']hidden[\"'])", re.I)


@pytest.fixture(scope="module")
def client(postgres_available: bool):
    from app import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as test_client:
        yield test_client


# --------------------------------------------------------------- 1. polling must not eat controls


@pytest.mark.parametrize("part", POLLED_PARTS)
def test_polled_partials_contain_no_form_controls(client: Any, part: str) -> None:
    """The invariant behind the Runs fix, enforced for every polled section, not just that one.

    Hidden inputs are exempt: they carry ids, not operator input, and losing one to a swap costs
    nothing because it is re-rendered with the same value.
    """
    body = client.get(f"/ops/parts/{part}").text

    found = _CONTROL.search(body)
    assert found is None, (
        f"/ops/parts/{part} polls on a timer and renders {found.group(0)!r}. HTMX replaces the whole "
        f"element on every tick, so that control resets under the operator. Move it into a partial "
        f"that is rendered once (see ops/_runs.html), or stop polling this section."
    )


def test_runs_controls_are_rendered_once_and_target_only_the_activity_half(client: Any) -> None:
    body = client.get("/ops/parts/runs").text

    assert "<select" in body, "the Runs controls partial should still hold the root/tier selects"
    # Every form posts into #runs-activity. Targeting #runs would replace the form being submitted.
    assert 'hx-target="#runs-activity"' in body
    assert 'hx-target="#runs"' not in body
    # The controls themselves must not be on a timer.
    controls_only = body.split('id="runs-activity"')[0]
    assert "hx-trigger" not in controls_only


def test_enqueueing_a_run_returns_the_activity_half_not_the_form(client: Any) -> None:
    """Submitting must not re-render the dropdown the operator just used."""
    response = client.post("/ops/runs", data={"action": "scan", "root_id": "", "tier": "2"})

    assert response.status_code == 200
    assert "<select" not in response.text
    assert "Requested" in response.text  # it is the request table that came back


def test_full_page_polls_the_activity_half_of_runs_only(client: Any) -> None:
    body = client.get("/ops").text

    activity = body.split('id="runs-activity"', 1)
    assert len(activity) == 2, "the full page should include the polled activity partial"
    assert "hx-get=\"/ops/parts/runs-activity\"" in body
    # The old wiring polled the entire section every 4 seconds.
    assert 'hx-get="/ops/parts/runs"' not in body


# ----------------------------------------------------------------- 2. long text is not the page


def test_a_long_error_is_truncated_with_the_full_text_one_click_away() -> None:
    """The macro, exercised directly - a real 2,000-character error is not reliably in the corpus."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(APP_DIR / "templates")))
    macros = env.get_template("ops/_macros.html").make_module()

    error = (
        "IntegrityError: (psycopg.errors.ForeignKeyViolation) insert or update on table "
        "\"metrics_snapshots\" violates foreign key constraint " + "x" * 1800
    )
    rendered = macros.longtext(error, limit=110)

    assert "<details" in rendered and "</details>" in rendered
    assert "show all" in rendered
    # The head is short enough to sit in a table cell...
    head = rendered.split("</span>", 1)[0]
    assert len(head) < 400
    # ...and nothing is lost: the whole string is still in the document, just hidden.
    assert error in rendered


def test_short_text_gets_no_disclosure_widget() -> None:
    """A triangle on a twelve-character message is noise, not affordance."""
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader(str(APP_DIR / "templates")))
    macros = env.get_template("ops/_macros.html").make_module()

    assert "<details" not in macros.longtext("queued", limit=110)
    assert "&mdash;" in macros.longtext(None)


def test_runs_table_routes_its_message_column_through_the_truncating_macro(client: Any) -> None:
    body = client.get("/ops/parts/runs-activity").text

    assert 'class="msg-cell"' in body, (
        "the Runs message/error column must render through ui.longtext - an unwrapped traceback in "
        "that cell is what pushed the rest of the table off screen"
    )


# ------------------------------------------------- 3. sections explain themselves and can be folded


@pytest.mark.parametrize("part", POLLED_PARTS[:1] + STATIC_PARTS)
def test_every_section_is_a_panel_that_remembers_whether_it_is_open(client: Any, part: str) -> None:
    body = client.get(f"/ops/parts/{part}").text

    assert 'class="panel"' in body
    assert "data-remember=" in body, (
        "an HTMX swap destroys the <details> open state; base.html restores it from localStorage "
        "keyed by data-remember, so a section without the attribute springs open on every poll"
    )


@pytest.mark.parametrize("part", POLLED_PARTS + STATIC_PARTS)
def test_every_section_carries_a_plain_english_help_note(client: Any, part: str) -> None:
    body = client.get(f"/ops/parts/{part}").text

    assert 'class="helptag"' in body, f"/ops/parts/{part} renders no help note"


def test_page_offers_pause_and_manual_refresh(client: Any) -> None:
    """Pausing is only tolerable if a manual refresh is one click away."""
    body = client.get("/ops").text

    assert 'id="pause-toggle"' in body
    assert 'id="refresh-now"' in body
    # The pause gate has to be on every poll, or pausing silently does nothing to some sections.
    poll_triggers = re.findall(r'hx-trigger="([^"]*every[^"]*)"', body)
    assert poll_triggers, "no polling triggers found on the page"
    for trigger in poll_triggers:
        assert "[opsLive()]" in trigger, f"polling trigger ignores the pause control: {trigger!r}"
        assert "ops:refresh" in trigger, f"polling trigger cannot be refreshed manually: {trigger!r}"


def test_polling_is_not_faster_than_the_data_can_change(client: Any) -> None:
    """`every 4s` on a section full of dropdowns was the original defect; keep the floor at 5s."""
    body = client.get("/ops").text

    intervals = [int(m) for m in re.findall(r"every (\d+)s", body)]
    assert intervals, "no polling intervals found"
    assert min(intervals) >= 5, f"a sub-5s poll is back on the page: {sorted(intervals)}"


# ------------------------------------------- 4. refreshing must not interrupt what you are doing


def test_the_refresh_gate_stands_down_while_the_operator_is_using_the_page(client: Any) -> None:
    """The fix for "my dropdown reset" and "my help note closed by itself".

    A refresh replaces a section's whole DOM, so anything open or focused inside it is destroyed.
    Choosing a slower interval does not fix that - it only makes the interruption rarer and more
    surprising. The gate has to consider the three things the operator can be in the middle of.
    """
    body = client.get("/ops").text

    assert "function opsBusy()" in body, "no interaction gate on the page"
    # Open disclosures: a help note, an expanded error, an expanded explanation.
    assert "details.helptag[open]" in body
    assert "details.longtext[open]" in body
    # A focused control. A native <select> holds focus while its list is open, which is exactly what
    # made a selection reset mid-use.
    assert '"select"' in body and '"textarea"' in body
    # A dashboard nobody is looking at should not be polled at all.
    assert "document.hidden" in body


def test_pause_and_busy_are_separate_reasons_to_skip_a_poll(client: Any) -> None:
    """Pressing pause is a decision; being mid-interaction is a temporary state.

    Collapsing them would mean either the button forgets itself the moment you touch a dropdown, or
    the amber "off" indicator lights up when nothing was actually turned off.
    """
    body = client.get("/ops").text

    gate = body.split("window.opsLive = function", 1)[1].split("};", 1)[0]
    assert "paused" in gate
    assert "opsBusy()" in gate


# ------------------------------------------------- 5. a stale tab has to admit that it is stale


def test_every_ops_response_carries_a_build_id_and_refuses_to_be_cached(client: Any) -> None:
    """A status page served from cache is a wrong page.

    This is not hypothetical: a tab left open across a rebuild polled a retired endpoint for half an
    hour while the rebuilt page it was meant to be showing was never requested once, so every fix
    appeared not to have worked.
    """
    for path in ("/ops", "/ops/parts/runs-activity", "/ops/parts/health"):
        response = client.get(path)
        assert response.status_code == 200
        assert "no-store" in response.headers.get("cache-control", ""), f"{path} may be cached"
        assert response.headers.get("x-ops-build"), f"{path} carries no build id"


def test_the_page_can_tell_it_has_gone_stale(client: Any) -> None:
    response = client.get("/ops")
    body = response.text

    # The page records the build it was served with...
    assert 'name="ops-build"' in body
    assert response.headers["x-ops-build"] in body
    # ...compares it against what the server says on each poll...
    assert "X-Ops-Build" in body
    # ...and has somewhere to say so.
    assert 'id="stale-banner"' in body
    assert "older version of the dashboard" in body


def test_the_build_id_changes_when_the_page_changes(tmp_path, monkeypatch) -> None:
    """A stamp that never moves would never warn anyone, so it is derived rather than declared.

    Driven against a temporary directory: the real template files belong to another user inside the
    test container, and a test that has to mutate the repository to prove a point is a test that will
    fail for an unrelated reason one day.
    """
    import importlib

    ops = importlib.import_module("routes.ops")

    templates = tmp_path / "templates" / "ops"
    templates.mkdir(parents=True)
    static = tmp_path / "static"
    static.mkdir()
    page = templates / "page.html"
    page.write_text("<p>one</p>", encoding="utf-8")
    (static / "ops.css").write_text("body{}", encoding="utf-8")

    monkeypatch.setattr(ops, "_TEMPLATES_DIR", tmp_path / "templates")
    monkeypatch.setattr(ops, "_STATIC_DIR", static)

    first = ops._ops_build()
    assert first and first != "0"

    # Same files, untouched -> same id. A stamp that drifted on its own would cry wolf every poll.
    assert ops._ops_build() == first

    # An edited template -> a different id.
    import os

    stat = page.stat()
    os.utime(page, (stat.st_atime, stat.st_mtime + 300))
    assert ops._ops_build() != first


def test_a_missing_asset_does_not_take_the_page_down(tmp_path, monkeypatch) -> None:
    """The stamp is a diagnostic. A diagnostic that can crash the thing it diagnoses is a liability."""
    import importlib

    ops = importlib.import_module("routes.ops")
    monkeypatch.setattr(ops, "_TEMPLATES_DIR", tmp_path / "does-not-exist")
    monkeypatch.setattr(ops, "_STATIC_DIR", tmp_path / "also-missing")

    assert isinstance(ops._ops_build(), str)
