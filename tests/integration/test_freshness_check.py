"""The "is the memory out of date?" check, against a real filesystem and a real database.

What this exists to prevent
---------------------------
Nothing watches the source folders. Edit a vault note and the system keeps answering from the copy it
read last time - confidently, with a citation, and with no warning - until somebody happens to run a
scan. Coverage cannot reveal it: it counts files the system already knows about, so an edited file
still reads as fully embedded. The only visible symptom is a wrong answer.

The check is therefore only worth having if it is *believed*, and it is only believed if two things
hold:

* it **finds** real changes, additions and removals; and
* it does **not** cry wolf - a file whose timestamp moved but whose bytes did not is not a change.

The second is the one a naive implementation gets wrong. `touch`, a sync client, a backup restore and
a git checkout all move mtime while leaving content identical. An indicator that reports those as work
trains the operator to ignore the number, which is worse than showing no number at all.

These tests build their own root in a temp directory rather than leaning on the pilot vault, so they
still mean something on a machine where nothing has been edited.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa

pytestmark = [pytest.mark.integration]

ROOT_ID = "freshness-test"


@pytest.fixture()
def scope(postgres_available: bool):
    from aimemory.common.config import get_settings
    from aimemory.persistence.db import Database

    database = Database(get_settings())

    class _Scope:
        def session(self):
            return database.session()

    return _Scope()


@pytest.fixture()
def temp_root(tmp_path: Path, monkeypatch, db_engine: sa.Engine):
    """A source root on disk, with three files already recorded in the database as current.

    Registered in `source_roots` too: `build_root_context` and the walk read the config object, but a
    foreign key ties `sources.root_id` to the table, so both have to agree.
    """
    from aimemory.domain.enums import SourceKind, SourceUriScheme
    from aimemory.domain.models import SourceRoot
    from aimemory.sources import freshness

    files = {
        "keep.md": "# unchanged\n\nthis file is never touched by the test\n",
        "edit.md": "# original wording\n\nthis line will be rewritten\n",
        "touch.md": "# identical\n\nonly the timestamp will move\n",
    }
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")

    root = SourceRoot(
        root_id=ROOT_ID,
        scheme=SourceUriScheme.LOCALFS,
        label=ROOT_ID,
        container_path=PurePosixPath(tmp_path.as_posix()),
        enabled=True,
        kind=SourceKind.DIRECTORY,
    )
    monkeypatch.setattr(freshness, "load_source_roots", lambda **_kwargs: [root])

    from aimemory.common.hashing import content_hash_bytes

    source_ids: list[Any] = []
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                INSERT INTO source_roots (root_id, scheme, label, container_path, device_id, enabled)
                VALUES (:rid, 'localfs', :rid, :cp, 'local-development-machine', true)
                ON CONFLICT (root_id) DO NOTHING
                """
            ),
            {"rid": ROOT_ID, "cp": tmp_path.as_posix()},
        )
        for name, body in files.items():
            stat = (tmp_path / name).stat()
            source_id, version_id = uuid4(), uuid4()
            source_ids.append(source_id)
            conn.execute(
                sa.text(
                    """
                    INSERT INTO sources (id, uri, root_id, relative_path, kind, media_type, policy,
                                         status, current_version_id, secret_suspected,
                                         first_seen_at, last_seen_at)
                    VALUES (:id, :uri, :root, :path, 'file', 'text/markdown', 'INDEX_CONTENT',
                            'active', :cur, false, now(), now())
                    """
                ),
                {
                    "id": source_id,
                    "uri": f"localfs://local-development-machine/{ROOT_ID}/{name}",
                    "root": ROOT_ID,
                    "path": name,
                    "cur": version_id,
                },
            )
            conn.execute(
                sa.text(
                    """
                    INSERT INTO source_versions (id, source_id, content_hash, size_bytes, mtime,
                                                 observed_at, is_current, change_type)
                    VALUES (:id, :sid, :hash, :size, :mtime, now(), true, 'new')
                    """
                ),
                {
                    "id": version_id,
                    "sid": source_id,
                    "hash": content_hash_bytes(body.encode("utf-8")),
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                },
            )

    yield tmp_path

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


def _check(scope: Any):
    from aimemory.sources.freshness import check_freshness

    return check_freshness(scope, root_id=ROOT_ID)


def test_a_quiet_folder_reports_nothing_to_do(scope: Any, temp_root: Path) -> None:
    """The baseline. If this is ever noisy, every other number here is worthless."""
    report = _check(scope)

    assert report.files_on_disk == 3
    assert report.unchanged == 3
    assert report.stale_files == 0
    assert report.is_stale is False
    assert report.errors == []


def test_an_edited_file_is_reported_and_named(scope: Any, temp_root: Path) -> None:
    (temp_root / "edit.md").write_text("# rewritten\n\ncompletely different content\n", encoding="utf-8")

    report = _check(scope)

    assert report.changed == 1
    assert report.stale_files == 1
    assert any("edit.md" in line for line in report.changed_sample), report.changed_sample


def test_a_touched_but_identical_file_is_not_reported_as_work(scope: Any, temp_root: Path) -> None:
    """The cry-wolf test, and the reason the check hashes instead of trusting mtime.

    `touch`, a sync client, a backup restore and a git checkout all move the timestamp without
    changing a byte. Counting those as changes would make the number meaningless within a week.
    """
    target = temp_root / "touch.md"
    future = (datetime.now(tz=timezone.utc) + timedelta(hours=6)).timestamp()
    os.utime(target, (future, future))

    report = _check(scope)

    assert report.changed == 0, "an identical file was reported as changed"
    assert report.touched_not_changed == 1, "the timestamp move should be noticed, then dismissed"
    assert report.hashed >= 1, "the file had to be read to prove it was unchanged"
    assert report.is_stale is False


def test_a_new_file_is_reported_as_new(scope: Any, temp_root: Path) -> None:
    (temp_root / "brand-new.md").write_text("# never seen before\n", encoding="utf-8")

    report = _check(scope)

    assert report.added == 1
    assert report.changed == 0
    assert any("brand-new.md" in line for line in report.added_sample)


def test_a_deleted_file_is_reported_as_removed(scope: Any, temp_root: Path) -> None:
    (temp_root / "keep.md").unlink()

    report = _check(scope)

    assert report.removed == 1
    assert any("keep.md" in line for line in report.removed_sample)


def test_the_check_reads_only_what_it_must(scope: Any, temp_root: Path) -> None:
    """Cheapness is a feature: this runs on a timer, so it must not hash a quiet corpus."""
    report = _check(scope)

    assert report.hashed == 0, (
        "nothing moved, so nothing should have been opened - if this hashes every file it costs as "
        "much as a scan and cannot run on a timer"
    )


# ------------------------------------------------------------------ what the dashboard then shows


def test_the_ops_page_reads_the_workers_finding_and_never_invents_one(scope: Any) -> None:
    """`memory-api` cannot see the source folders, so it must report honestly when nothing was recorded.

    "0 files changed" from a check that never ran is the most dangerous sentence this page could
    print - it says "up to date" about a system nobody looked at.
    """
    from aimemory.ops.queries import freshness_view

    with scope.session() as session:
        view = freshness_view(session)

    if not view.has_check:
        assert "Not checked yet" in view.headline
        assert view.is_stale is False
        return

    assert view.files_on_disk >= 0
    assert view.checked_at is not None
    assert view.headline


def test_the_headline_says_what_to_do() -> None:
    from aimemory.ops.viewmodels import FreshnessView

    assert "Not checked yet" in FreshnessView().headline
    assert "Up to date" in FreshnessView(has_check=True).headline

    busy = FreshnessView(has_check=True, changed=3, added=1, removed=2)
    assert busy.stale_files == 6
    assert "3 changed" in busy.headline
    assert "1 new" in busy.headline
    assert "2 removed" in busy.headline
    assert "run a scan" in busy.headline


def test_an_old_check_is_flagged_rather_than_trusted() -> None:
    """The worker re-checks every two minutes; a much older reading means it is probably down."""
    from aimemory.common.time import utc_now
    from aimemory.ops.viewmodels import FreshnessView

    fresh = FreshnessView(has_check=True, checked_at=utc_now())
    assert fresh.check_is_old is False

    stale_reading = FreshnessView(has_check=True, checked_at=utc_now() - timedelta(hours=3))
    assert stale_reading.check_is_old is True
