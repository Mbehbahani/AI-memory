"""A search must see exactly one version of any file: the one in force at ``as_of``.

The defect this pins
--------------------
``CHUNK_PREDICATES`` scoped by project, source status, policy, secret flag and ``since`` - never by
version. Every version of a file that had ever been indexed stayed retrievable, with its own
embeddings, forever. MEASURED on the live corpus before the fix: one edited vault document had 4
chunks from its 04:45 version and 4 from its 18:20 version, all 8 embedded and all 8 searchable, with
nothing marking either as superseded. A search could quote text that no longer existed on disk and
cite it as current.

The sharp part: **re-scanning did not repair it, re-scanning caused it.** Diligence made it worse, so
there was no usage pattern that would have surfaced it. Nothing failed, nothing was logged, and the
answers stayed plausible - which is exactly the shape of bug that survives a test suite checking that
data is *present*.

These tests build their own two-version source rather than leaning on whatever the pilot corpus
happens to contain, so they keep failing if the predicate is ever removed - including on a fresh
database where no file has been edited yet.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

pytestmark = [pytest.mark.integration]

#: Nonsense on purpose: two words that cannot collide with anything in the real corpus, so a hit is
#: unambiguously one of the rows this test inserted.
OLD_TEXT = "zarquon telemetry blivet was the superseded wording of this paragraph"
NEW_TEXT = "zarquon telemetry blivet is the current wording of this paragraph"


@pytest.fixture()
def two_version_source(db_engine: sa.Engine):
    """A source with an old and a current version, each holding one chunk.

    Committed for real - the retrieval code opens its own session, so an uncommitted fixture
    transaction would be invisible to it. Removed again in teardown, children first.
    """
    source_id, old_version, new_version = uuid4(), uuid4(), uuid4()
    old_chunk, new_chunk = uuid4(), uuid4()
    root_id = "vault"

    with db_engine.begin() as conn:
        project_id = conn.execute(sa.text("SELECT id FROM projects LIMIT 1")).scalar()
        now = conn.execute(sa.text("SELECT now()")).scalar()

        conn.execute(
            sa.text(
                """
                INSERT INTO sources (id, uri, root_id, relative_path, project_id, kind, media_type,
                                     policy, status, current_version_id, secret_suspected,
                                     first_seen_at, last_seen_at)
                VALUES (:id, :uri, :root, :path, :project, 'file', 'text/markdown',
                        'INDEX_CONTENT', 'active', :cur, false, :now, :now)
                """
            ),
            {
                "id": source_id,
                "uri": f"test://version-currency/{source_id}",
                "root": root_id,
                "path": f"__test__/version-currency-{source_id}.md",
                "project": project_id,
                "cur": new_version,
                "now": now,
            },
        )
        for version_id, offset, is_current, change in (
            (old_version, timedelta(hours=-6), False, "new"),
            (new_version, timedelta(0), True, "modified"),
        ):
            conn.execute(
                sa.text(
                    """
                    INSERT INTO source_versions (id, source_id, content_hash, size_bytes,
                                                 observed_at, is_current, change_type)
                    VALUES (:id, :sid, :hash, 100, :at, :cur, :ct)
                    """
                ),
                {
                    "id": version_id,
                    "sid": source_id,
                    "hash": str(version_id),
                    "at": now + offset,
                    "cur": is_current,
                    "ct": change,
                },
            )
        for chunk_id, version_id, body in (
            (old_chunk, old_version, OLD_TEXT),
            (new_chunk, new_version, NEW_TEXT),
        ):
            conn.execute(
                sa.text(
                    """
                    INSERT INTO chunks (id, source_id, version_id, project_id, ordinal, text,
                                        text_hash, char_start, char_end, token_count)
                    VALUES (:id, :sid, :vid, :project, 0, :text, :hash, 0, :len, 20)
                    """
                ),
                {
                    "id": chunk_id,
                    "sid": source_id,
                    "vid": version_id,
                    "project": project_id,
                    "text": body,
                    "hash": str(chunk_id),
                    "len": len(body),
                },
            )

    yield {
        "source_id": source_id,
        "old_version": old_version,
        "new_version": new_version,
        "old_chunk": old_chunk,
        "new_chunk": new_chunk,
        "now": now,
    }

    with db_engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM chunks WHERE source_id = :sid"), {"sid": source_id})
        conn.execute(
            sa.text("UPDATE sources SET current_version_id = NULL WHERE id = :sid"),
            {"sid": source_id},
        )
        conn.execute(
            sa.text("DELETE FROM source_versions WHERE source_id = :sid"), {"sid": source_id}
        )
        conn.execute(sa.text("DELETE FROM sources WHERE id = :sid"), {"sid": source_id})


def _keyword_chunk_ids(session: Any, query_text: str, **overrides: Any) -> set[UUID]:
    """Run the real keyword candidate SQL through the real filters - no hand-written query here.

    A test that rebuilt the predicate itself would pass while the shipped one stayed broken.
    """
    from aimemory.domain.retrieval import SearchQuery
    from aimemory.retrieval import candidates
    from aimemory.retrieval.filters import ScopeFilters

    scope = ScopeFilters.from_query(SearchQuery(query=query_text, **overrides))
    rows = session.execute(
        sa.text(candidates._KEYWORD_CHUNKS_SQL),
        {**scope.bind_params(), "query_text": query_text, "k": 50},
    ).mappings()
    return {row["object_id"] for row in rows}


def test_only_the_current_version_of_a_file_is_searchable(
    db_engine: sa.Engine, two_version_source: dict
) -> None:
    """The headline guarantee. Before the fix both chunks came back."""
    with db_engine.connect() as conn:
        found = _keyword_chunk_ids(conn, "zarquon telemetry blivet")

    assert two_version_source["new_chunk"] in found, "the current version must be retrievable"
    assert two_version_source["old_chunk"] not in found, (
        "a chunk from a superseded version was returned. Search would quote text that is no longer "
        "in the file and cite it as current."
    )


def test_exactly_one_version_is_returned_not_merely_the_newest(
    db_engine: sa.Engine, two_version_source: dict
) -> None:
    """Both rows match the query text, so a passing count of 1 is the predicate doing its job."""
    with db_engine.connect() as conn:
        found = _keyword_chunk_ids(conn, "zarquon telemetry blivet")

    assert len(found) == 1, f"expected one version of the test file, got {len(found)}"


def test_an_as_of_query_returns_the_text_that_was_in_the_file_then(
    db_engine: sa.Engine, two_version_source: dict
) -> None:
    """Why the fix is not simply ``AND sv.is_current``.

    ``as_of`` asks "what did I know at time T". Facts and artifacts already honour it through
    ``valid_from``/``valid_to``; answering it with today's file contents would be a different kind of
    wrong. Three hours ago, the old wording *was* the file.
    """
    three_hours_ago = two_version_source["now"] - timedelta(hours=3)
    with db_engine.connect() as conn:
        found = _keyword_chunk_ids(conn, "zarquon telemetry blivet", as_of=three_hours_ago)

    assert two_version_source["old_chunk"] in found, "as_of should see the version in force then"
    assert two_version_source["new_chunk"] not in found, "as_of must not see the future"
    assert len(found) == 1


def test_as_of_before_the_file_existed_returns_nothing(
    db_engine: sa.Engine, two_version_source: dict
) -> None:
    """A file not yet observed has no text, rather than falling back to its earliest version."""
    with db_engine.connect() as conn:
        found = _keyword_chunk_ids(
            conn, "zarquon telemetry blivet", as_of=two_version_source["now"] - timedelta(days=30)
        )

    assert not (found & {two_version_source["old_chunk"], two_version_source["new_chunk"]})


def test_the_predicate_is_in_the_shipped_chunk_filter(two_version_source: dict) -> None:
    """Guards against the predicate being defined but never wired in - the original failure mode.

    ``source_versions.is_current`` was maintained correctly all along; retrieval simply never asked.
    """
    from aimemory.retrieval.filters import CHUNK_PREDICATES, CURRENT_VERSION_PREDICATE

    assert CURRENT_VERSION_PREDICATE.strip() in CHUNK_PREDICATES


def test_the_live_corpus_has_no_source_answering_with_two_versions(db_engine: sa.Engine) -> None:
    """The regression in its original form, against whatever is really in the database.

    Multi-version sources are expected and healthy - editing a file is normal. What must never happen
    is more than one of them being *visible to a search at the same instant*.

    Grouped by source id, not by relative path: a path is unique only within a root, and `AGENTS.md`
    exists in both `vault` and `joblab-de`. Grouping by path read those two separate files as one
    file at two versions and failed on a corpus that was in fact correct.
    """
    with db_engine.connect() as conn:
        leaked = conn.execute(
            sa.text(
                """
                SELECT s.root_id, s.relative_path, count(DISTINCT c.version_id) AS visible_versions
                  FROM chunks c
                  JOIN sources s          ON s.id = c.source_id
                  JOIN source_versions sv ON sv.id = c.version_id
                 WHERE sv.observed_at <= now()
                   AND NOT EXISTS (
                       SELECT 1 FROM source_versions sv2
                        WHERE sv2.source_id = sv.source_id
                          AND sv2.observed_at <= now()
                          AND (sv2.observed_at, sv2.id) > (sv.observed_at, sv.id)
                   )
                 GROUP BY s.id, s.root_id, s.relative_path
                HAVING count(DISTINCT c.version_id) > 1
                """
            )
        ).all()

    assert not leaked, f"sources visible at more than one version: {leaked}"
