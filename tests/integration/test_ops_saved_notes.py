"""The "what have I saved?" panel.

Why it had to exist
-------------------
Nothing in the system could answer it. `/v1/sources` lists files the scanner discovered.
`memory_search` needs embeddings - and `Gateway.add_episode` writes an episode and **no chunks**, so
a session recorded through MCP is real, extracted knowledge whose sentences exist in no index at all.
Searching `types=["episode"]` returns zero hits, because there is nothing to match against.

The consequence, put concretely: with twenty sessions saved through MCP, the only way to discover
that any of them existed was to query Postgres by hand. Memory you cannot enumerate is memory you
will not trust.

What these tests hold
---------------------
* every hand-written or agent-written entry appears, including ones with no file behind them;
* `searchable` is honest per row - it is the column that tells the operator *which kind* of
  saved knowledge they are looking at, and getting it backwards would be worse than omitting it;
* ordinary scanned documents stay out, or the panel becomes a second copy of Coverage.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa

from aimemory.common.time import utc_now

pytestmark = [pytest.mark.integration]


@pytest.fixture()
def session(postgres_available: bool):
    from aimemory.common.config import get_settings
    from aimemory.persistence.db import Database

    with Database(get_settings()).session() as s:
        yield s


@pytest.fixture()
def written_episodes(db_engine: sa.Engine):
    """One MCP write and one manual write, neither backed by a file."""
    ids = {"mcp": uuid4(), "manual": uuid4()}
    at = utc_now() + timedelta(seconds=5)  # newest, so they land at the top of the list
    with db_engine.begin() as conn:
        for kind, episode_id in ids.items():
            conn.execute(
                sa.text(
                    """
                    INSERT INTO episodes (id, type, title, body, observed_at, status, priority,
                                          origin, tier)
                    VALUES (:id, :type, :title, :body, :at, 'extracted', 50, 'internal', 2)
                    """
                ),
                {
                    "id": episode_id,
                    "type": kind,
                    "title": f"saved-notes probe {kind}",
                    "body": f"a {kind} write with no file behind it",
                    "at": at,
                },
            )
    yield ids
    with db_engine.begin() as conn:
        conn.execute(
            sa.text("DELETE FROM episodes WHERE id = ANY(CAST(:ids AS uuid[]))"),
            {"ids": [str(i) for i in ids.values()]},
        )


def _rows(session):
    from aimemory.ops.queries import notes_view

    return notes_view(session).rows


# ------------------------------------------------------------------ it lists what nothing else can


def test_an_mcp_write_appears_even_though_it_has_no_file(session, written_episodes) -> None:
    """The case the panel exists for. No source, no chunks, invisible to every other view."""
    titles = [r.title for r in _rows(session)]

    assert "saved-notes probe mcp" in titles


def test_a_manual_write_appears_too(session, written_episodes) -> None:
    titles = [r.title for r in _rows(session)]

    assert "saved-notes probe manual" in titles


def test_the_newest_entries_come_first(session, written_episodes) -> None:
    """A list of saved sessions is read from the top; oldest-first would bury today's work."""
    rows = _rows(session)

    assert [r.at for r in rows] == sorted((r.at for r in rows), reverse=True)


# ------------------------------------------------------------------ the column that matters


def test_a_write_with_no_chunks_is_reported_as_not_text_searchable(session, written_episodes) -> None:
    """The honest half of the panel.

    `add_episode` never produces chunks, so the *facts* drawn from the text answer questions while
    the text itself cannot be found. Reporting these as searchable would send the operator hunting
    for a passage that is not in any index.
    """
    probe = next(r for r in _rows(session) if r.title == "saved-notes probe mcp")

    assert probe.searchable is False


def test_the_view_counts_how_many_are_unreachable_by_text_search(session, written_episodes) -> None:
    """A per-row flag nobody totals is a flag nobody acts on."""
    from aimemory.ops.queries import notes_view

    view = notes_view(session)

    assert view.unsearchable >= 2
    assert view.unsearchable <= view.total
    assert view.total == len(view.rows)


def test_a_session_note_saved_as_a_vault_file_is_searchable(session) -> None:
    """The contrast that makes the column meaningful.

    Skipped rather than faked when no session note has been saved yet: inventing a source row with
    chunks would test the fixture, not the behaviour.
    """
    notes = [r for r in _rows(session) if r.kind == "session note"]
    if not notes:
        pytest.skip("no session note saved in the vault yet")

    assert any(r.searchable for r in notes), (
        "a note that went through the scanner has chunks, so its words are findable - that is the "
        "whole reason to save a session as a file as well as through MCP"
    )


# ------------------------------------------------------------------ what it must NOT become


def test_ordinary_scanned_documents_are_excluded(session) -> None:
    """Otherwise this is Coverage again, and the few entries that need listing drown in 200 files."""
    from aimemory.ops.queries import SESSION_NOTE_PATH

    rows = _rows(session)
    prefix = SESSION_NOTE_PATH.rstrip("%")

    for row in rows:
        if row.kind == "session note":
            assert row.source_uri and prefix.split("/")[-2] in (row.source_uri or ""), (
                "only notes under the session-note path may enter as documents"
            )
        else:
            assert row.kind in {"mcp", "manual"}


def test_the_panel_survives_an_empty_corpus() -> None:
    """A fresh install must render an explanation, not a crash or a bare empty table."""
    from aimemory.ops.viewmodels import NotesView

    empty = NotesView()

    assert empty.has_any is False
    assert empty.total == 0
    assert empty.unsearchable == 0
