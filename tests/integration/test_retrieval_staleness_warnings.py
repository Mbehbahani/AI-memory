"""Does a search admit when its answer may be old?

What this exists to prevent
---------------------------
A chunk does not stop being searchable when the folder behind it stops being watched. Two ways that
happens:

* **The root is disabled.** Scans stop, and - because the freshness check also skips disabled roots -
  the "files changed" indicator stops too. Retrieval has no such check, so the chunks keep ranking
  normally. Content frozen at some past moment, served with full confidence, with the one indicator
  that would have caught it deliberately looking away.
* **The corpus has moved.** The last freshness check found changed files, so some answer somewhere is
  out of date. Which one is not knowable from here.

Both were already visible in ``provenance.observed_at``. Neither was ever *said*. The distinction
matters because a caller - human or model - reads the prose and skims the metadata, and the failure
mode is a confident code change made against a repository as it looked last week.

The rule these tests hold to: **warn, never filter.** A frozen snapshot is still the best available
answer to "what did this look like?", and silently returning fewer results is the invisible
degradation ``SearchResult.warnings`` exists to prevent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import sqlalchemy as sa

from aimemory.retrieval.staleness import (
    DISABLED_ROOT_WARNING,
    STALE_CORPUS_WARNING,
    _FRESHNESS_SERVICE,
    staleness_warnings,
)

pytestmark = [pytest.mark.integration]

ROOT_ID = "staleness-test"


@pytest.fixture()
def frozen_root(db_engine: sa.Engine):
    """A disabled source root with one file recorded against it.

    Disabled *and* populated is the combination that matters: enabling has no effect on retrieval, so
    only a root that still contributes hits can produce the warning.
    """
    source_id, version_id = uuid4(), uuid4()
    observed = datetime(2026, 9, 18, 3, 41, 58, tzinfo=timezone.utc)

    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO source_roots (root_id, scheme, label, container_path, device_id, enabled)
                VALUES (:rid, 'localfs', :rid, '/sources/frozen', 'local-development-machine', false)
                ON CONFLICT (root_id) DO UPDATE SET enabled = false
                """
            ),
            {"rid": ROOT_ID},
        )
        conn.execute(
            sa.text(
                """
                INSERT INTO sources (id, uri, root_id, relative_path, kind, media_type, policy,
                                     status, current_version_id, secret_suspected,
                                     first_seen_at, last_seen_at)
                VALUES (:id, :uri, :root, 'README.md', 'file', 'text/markdown', 'INDEX_CONTENT',
                        'active', :cur, false, now(), now())
                """
            ),
            {
                "id": source_id,
                "uri": f"localfs://local-development-machine/{ROOT_ID}/README.md",
                "root": ROOT_ID,
                "cur": version_id,
            },
        )
        conn.execute(
            sa.text(
                """
                INSERT INTO source_versions (id, source_id, content_hash, size_bytes, mtime,
                                             observed_at, is_current, change_type)
                VALUES (:id, :sid, 'sha256:frozen', 10, :observed, :observed, true, 'new')
                """
            ),
            {"id": version_id, "sid": source_id, "observed": observed},
        )

    yield source_id, observed

    with db_engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE sources SET current_version_id = NULL WHERE root_id = :r"), {"r": ROOT_ID}
        )
        conn.execute(
            sa.text(
                "DELETE FROM source_versions WHERE source_id IN "
                "(SELECT id FROM sources WHERE root_id = :r)"
            ),
            {"r": ROOT_ID},
        )
        conn.execute(sa.text("DELETE FROM sources WHERE root_id = :r"), {"r": ROOT_ID})
        conn.execute(sa.text("DELETE FROM source_roots WHERE root_id = :r"), {"r": ROOT_ID})


@pytest.fixture()
def session(postgres_available: bool):
    from aimemory.common.config import get_settings
    from aimemory.persistence.db import Database

    database = Database(get_settings())
    with database.session() as s:
        yield s


# ------------------------------------------------------------------ a root nobody is watching


def test_a_hit_from_a_disabled_root_is_called_out_by_name_and_date(session, frozen_root) -> None:
    """The warning has to be actionable, so it names the root and when the content was last read."""
    source_id, observed = frozen_root

    warnings = staleness_warnings(session, [source_id])

    frozen = [w for w in warnings if ROOT_ID in w]
    assert frozen, f"a disabled root produced no warning: {warnings}"
    assert observed.isoformat() in frozen[0], "the warning must say how old the content is"
    assert "1 result" in frozen[0]


def test_the_count_reflects_the_hits_actually_returned(session, frozen_root) -> None:
    """Two hits from the same frozen root are one warning saying "2", not two warnings."""
    source_id, _ = frozen_root

    warnings = [w for w in staleness_warnings(session, [source_id, source_id]) if ROOT_ID in w]

    assert len(warnings) == 1
    assert "2 result" in warnings[0]


def test_a_disabled_root_that_contributed_nothing_is_not_mentioned(session, frozen_root) -> None:
    """A warning about something the caller cannot see is noise, and noise is how warnings die.

    The root is disabled for the whole duration of this test - it just did not produce a hit.
    """
    warnings = staleness_warnings(session, [])

    assert not [w for w in warnings if ROOT_ID in w]


def test_an_enabled_root_produces_no_warning(session, frozen_root, db_engine) -> None:
    """The control. If this ever warns, every answer carries the warning and none of them mean it."""
    source_id, _ = frozen_root
    with db_engine.begin() as conn:
        conn.execute(
            sa.text("UPDATE source_roots SET enabled = true WHERE root_id = :r"), {"r": ROOT_ID}
        )

    assert not [w for w in staleness_warnings(session, [source_id]) if ROOT_ID in w]


# ------------------------------------------------------------------ a corpus that has moved


def _record_freshness(engine: sa.Engine, stale_files: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO service_stats (id, at, service, details)
                VALUES (:id, :at, :service, CAST(:details AS jsonb))
                """
            ),
            {
                "id": uuid4(),
                "at": datetime.now(tz=timezone.utc) + timedelta(seconds=5),
                "service": _FRESHNESS_SERVICE,
                "details": f'{{"stale_files": {stale_files}, "changed": {stale_files}}}',
            },
        )


def test_changed_files_on_disk_are_reported_as_a_count_not_a_guess(session, db_engine) -> None:
    """Which hit is stale is not knowable here, so the warning names a number and no filename.

    A warning that names the wrong file is worse than one that names none: it sends the reader to
    check something that was never the problem.
    """
    _record_freshness(db_engine, 3)

    warnings = [w for w in staleness_warnings(session, []) if "changed since the last scan" in w]

    assert warnings, "a corpus known to have moved produced no warning"
    assert "3 file(s)" in warnings[0]
    assert "run a scan" in warnings[0].lower()


def test_a_quiet_corpus_says_nothing(session, db_engine) -> None:
    _record_freshness(db_engine, 0)

    assert not [w for w in staleness_warnings(session, []) if "changed since the last scan" in w]


# ------------------------------------------------------------------ it may never break a search


def test_the_check_never_raises(session) -> None:
    """A search must not fail because the thing annotating it did."""
    assert staleness_warnings(session, []) is not None
    assert staleness_warnings(session, [uuid4()]) is not None  # a source id that does not exist

    class _Broken:
        def execute(self, *_args, **_kwargs):
            raise RuntimeError("database went away")

    assert staleness_warnings(_Broken(), [uuid4()]) == []


def test_the_freshness_service_name_matches_the_writer() -> None:
    """``staleness`` copies the service name rather than importing the ingestion pipeline into a read
    path. That is only safe while the two agree, so this is the thing that keeps them agreeing."""
    from aimemory.sources.freshness import SERVICE

    assert _FRESHNESS_SERVICE == SERVICE


def test_the_warnings_read_as_instructions_not_diagnostics() -> None:
    """These reach a model as context. Each must say what to do, not merely what is wrong."""
    disabled = DISABLED_ROOT_WARNING.format(count=3, root_id="joblab-de", observed_at="2026-09-18")
    assert "Open the file" in disabled
    assert "frozen" in disabled

    stale = STALE_CORPUS_WARNING.format(count=2)
    assert "Run a scan" in stale


# ------------------------------------------------------------------ the drift that hid all of this


def test_the_worker_writes_root_config_into_the_table_on_startup(db_engine) -> None:
    """Without this, disabling a root never reaches the database and no warning can ever fire.

    ``sync_source_roots`` otherwise runs only inside a scan and from ``aimemory-ingest roots --sync``,
    so a *disable* could not record itself: the edit stops the scans that would have written it. The
    scanner reads the YAML and behaved correctly; every reader of the table went on seeing
    ``enabled = true``.

    MEASURED 2026-09-19: ``joblab-de`` was disabled in config on the 18th and the table still read
    ``enabled`` a day later - so the one root nothing was watching was the one search could not warn
    about.
    """
    from aimemory.common.config import get_settings
    from aimemory.persistence.db import Database
    from aimemory.sources.worker import IngestionWorker

    # Put the table deliberately out of step with config, the way a disable does.
    with db_engine.begin() as conn:
        conn.execute(sa.text("UPDATE source_roots SET enabled = true WHERE enabled IS FALSE"))

    settings = get_settings()
    database = Database(settings)

    worker = IngestionWorker.__new__(IngestionWorker)
    worker._settings = settings
    worker._scope = database

    worker._sync_root_config()

    from aimemory.sources.roots import load_source_roots

    # include_disabled=True, or this compares only the enabled roots and proves nothing about the
    # case that matters - a root disabled in config that the table still reports as enabled.
    expected = {
        r.root_id: r.enabled
        for r in load_source_roots(settings=settings, include_disabled=True)
    }
    assert False in expected.values(), (
        "this test needs at least one disabled root in config to mean anything"
    )
    with db_engine.begin() as conn:
        actual = dict(conn.execute(sa.text("SELECT root_id, enabled FROM source_roots")).all())

    for root_id, enabled in expected.items():
        assert actual.get(root_id) == enabled, f"{root_id}: table disagrees with config"


def test_the_startup_sync_never_stops_the_worker() -> None:
    """The queue must keep draining even if the config file is unreadable or the write fails."""
    from aimemory.sources.worker import IngestionWorker

    class _Broken:
        def session(self):
            raise RuntimeError("database went away")

    worker = IngestionWorker.__new__(IngestionWorker)
    worker._settings = None
    worker._scope = _Broken()

    worker._sync_root_config()  # must return quietly
