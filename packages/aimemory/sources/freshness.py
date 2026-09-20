"""Answers one question without changing anything: **is the memory out of date?**

The gap this closes
-------------------
Nothing watches the source folders. Edit a vault note and the system does not notice - it keeps
answering from the copy it read last time, confidently and with no warning, until somebody happens to
run a scan. Coverage on the Ops page does not help: it counts files the system already knows about, so
an edited file still reads as fully embedded. The only visible symptom is a wrong answer, which is the
worst possible way to find out.

So this module compares what is on disk against what is in the database and reports the difference.
It writes nothing to ``sources``, reads no file content it does not have to, and never queues work.
Its whole output is a count and a handful of example paths.

Why it is cheap enough to run on a timer
----------------------------------------
Hashing every file on every check would make a status indicator cost as much as a scan. Instead this
runs in two passes:

1. **stat only** - ``walk_root`` already yields size and mtime without opening anything. A file whose
   size *and* mtime both match the recorded version is unchanged. That settles the overwhelming
   majority (MEASURED on the pilot corpus: 241 of 241 files on a quiet day).
2. **hash the survivors** - anything whose size or mtime differs is hashed and compared properly.

Step 2 is what keeps this honest. mtime is not evidence of a change: `touch`, a sync client, a backup
restore or a git checkout all move it while leaving the bytes identical. Reporting those as "changed"
would train the operator to ignore the number, which is worse than not showing one. Hashing the few
candidates costs almost nothing and turns a guess into a measurement - the same rule the rest of this
system follows.

Where it runs, and why not in the web app
-----------------------------------------
In the **ingestion worker**, which is the only container with the source folders mounted (read-only).
``memory-api`` deliberately cannot see them - it is the one service exposed over HTTP, and widening
what it can read to power a status widget would be a poor trade. The worker records its finding as a
``service_stats`` row; the Ops page reads that row and does no filesystem work at all. Same division
as everywhere else here: the worker owns the disk, the API owns HTTP.

That means the number on the page is *as of* the last check, never live - and it is displayed with its
timestamp for exactly that reason.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from ..common.config import Settings, get_settings
from ..common.ids import new_id
from ..common.logging import get_logger
from ..common.time import utc_now
from ..domain.models import ServiceStat
from ..persistence.repositories import MetricsRepo
from .discovery import walk_root
from .fingerprint import fingerprint_file
from .pipeline import SessionScope
from .roots import build_root_context, load_source_roots

__all__ = ["FreshnessReport", "check_freshness", "write_freshness_stat"]

logger = get_logger(__name__)

#: Service name for the ``service_stats`` row. Distinct from ``ingestion`` so the worker's own health
#: snapshot and this check never overwrite each other's latest row.
SERVICE = "freshness"

#: How many example paths to keep per category. Enough to recognise what changed, short enough that
#: the row stays small and the page stays readable.
SAMPLE_LIMIT = 10

#: Files below this are read whole when hashing; larger ones stream. Matches the ingestion default so
#: a hash computed here is byte-for-byte the hash a scan would compute.
_MAX_READ_BYTES = 8 * 1024 * 1024


@dataclass
class FreshnessReport:
    """What the last check found. Counts first; paths are examples, not the full list."""

    checked_at: datetime = field(default_factory=utc_now)
    roots_checked: list[str] = field(default_factory=list)
    files_on_disk: int = 0
    unchanged: int = 0
    changed: int = 0
    added: int = 0
    removed: int = 0
    #: Files whose size/mtime moved but whose bytes did not. Reported separately because they are
    #: *not* work to do - counting them as changes would inflate the number the operator acts on.
    touched_not_changed: int = 0
    hashed: int = 0
    duration_ms: int = 0
    changed_sample: list[str] = field(default_factory=list)
    added_sample: list[str] = field(default_factory=list)
    removed_sample: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def stale_files(self) -> int:
        """Files a scan would actually process. The one number worth putting on a button."""
        return self.changed + self.added + self.removed

    @property
    def is_stale(self) -> bool:
        return self.stale_files > 0

    def as_details(self) -> dict[str, Any]:
        """The ``service_stats.details`` payload. Flat and JSON-safe on purpose."""
        return {
            "checked_at": self.checked_at.isoformat(),
            "roots_checked": self.roots_checked,
            "files_on_disk": self.files_on_disk,
            "unchanged": self.unchanged,
            "changed": self.changed,
            "added": self.added,
            "removed": self.removed,
            "touched_not_changed": self.touched_not_changed,
            "hashed": self.hashed,
            "duration_ms": self.duration_ms,
            "stale_files": self.stale_files,
            "changed_sample": self.changed_sample,
            "added_sample": self.added_sample,
            "removed_sample": self.removed_sample,
            "errors": self.errors[:5],
        }


_KNOWN_SQL = """
    SELECT s.root_id       AS root_id,
           s.relative_path AS relative_path,
           sv.content_hash AS content_hash,
           sv.size_bytes   AS size_bytes,
           sv.mtime        AS mtime
      FROM sources s
      JOIN source_versions sv ON sv.id = s.current_version_id
     WHERE s.status <> 'deleted'
       AND s.root_id = ANY(CAST(:root_ids AS text[]))
"""


def _known_versions(scope: SessionScope, root_ids: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(root_id, relative_path) -> current version facts}``.

    Keyed on the pair, not the path alone: a path is unique only within a root, and `AGENTS.md`
    exists in more than one of them.
    """
    with scope.session() as session:
        rows = session.execute(text(_KNOWN_SQL), {"root_ids": root_ids}).mappings().all()
    return {(row["root_id"], row["relative_path"]): dict(row) for row in rows}


def _same_stat(known: dict[str, Any], size_bytes: int, mtime: datetime) -> bool:
    """Cheap equality: identical size *and* mtime means untouched, so no hashing is needed.

    mtime is compared to the second. Filesystems, Postgres and Python disagree below that, and a
    sub-second difference has never meant a real edit - only a rounding one.
    """
    if int(known.get("size_bytes") or -1) != int(size_bytes):
        return False
    recorded = known.get("mtime")
    if recorded is None:
        return False
    try:
        return abs((recorded - mtime).total_seconds()) < 1.0
    except TypeError:  # pragma: no cover - naive/aware mismatch, treat as "needs hashing"
        return False


def check_freshness(
    scope: SessionScope, settings: Settings | None = None, *, root_id: str | None = None
) -> FreshnessReport:
    """Compare disk against the database. Reads nothing it does not need to; writes nothing at all."""
    settings = settings or get_settings()
    started = time.monotonic()
    report = FreshnessReport()

    roots = [r for r in load_source_roots(settings=settings) if r.enabled]
    if root_id is not None:
        roots = [r for r in roots if r.root_id == root_id]
    if not roots:
        report.duration_ms = int((time.monotonic() - started) * 1000)
        return report

    report.roots_checked = [r.root_id for r in roots]
    known = _known_versions(scope, report.roots_checked)
    seen: set[tuple[str, str]] = set()

    for root in roots:
        try:
            ctx = build_root_context(root, settings=settings)
        except Exception as exc:  # noqa: BLE001 - an unmounted root must not fail the whole check
            report.errors.append(f"{root.root_id}: {type(exc).__name__}: {exc}"[:200])
            logger.warning("freshness.root_unavailable", root=root.root_id, error=str(exc)[:200])
            continue

        try:
            for found in walk_root(ctx):
                key = (root.root_id, found.relative_path)
                seen.add(key)
                report.files_on_disk += 1
                record = known.get(key)

                if record is None:
                    report.added += 1
                    if len(report.added_sample) < SAMPLE_LIMIT:
                        report.added_sample.append(f"{root.root_id}: {found.relative_path}")
                    continue

                # Pass 1: stat says nothing moved, so nothing was written. No read required.
                if _same_stat(record, found.size_bytes, found.mtime):
                    report.unchanged += 1
                    continue

                # Pass 2: something moved. Hash it rather than assume - `touch`, a sync client or a
                # git checkout all move mtime without changing a byte.
                try:
                    fingerprint = fingerprint_file(
                        Path(found.path),
                        size_bytes=found.size_bytes,
                        mtime=found.mtime,
                        max_read_bytes=_MAX_READ_BYTES,
                    )
                except OSError as exc:
                    report.errors.append(f"{found.relative_path}: {type(exc).__name__}")
                    continue
                report.hashed += 1

                if fingerprint.content_hash == record.get("content_hash"):
                    report.touched_not_changed += 1
                    report.unchanged += 1
                else:
                    report.changed += 1
                    if len(report.changed_sample) < SAMPLE_LIMIT:
                        report.changed_sample.append(f"{root.root_id}: {found.relative_path}")
        except Exception as exc:  # noqa: BLE001 - a status check must never take the worker down
            report.errors.append(f"{root.root_id}: {type(exc).__name__}: {exc}"[:200])
            logger.warning("freshness.walk_failed", root=root.root_id, error=str(exc)[:200])

    # Anything the database holds for a checked root that the walk did not reach: deleted, renamed,
    # or newly excluded by .memoryignore. All three are things a scan would act on.
    for (known_root, relative_path) in known:
        if (known_root, relative_path) in seen:
            continue
        report.removed += 1
        if len(report.removed_sample) < SAMPLE_LIMIT:
            report.removed_sample.append(f"{known_root}: {relative_path}")

    report.duration_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "freshness.checked",
        files=report.files_on_disk,
        changed=report.changed,
        added=report.added,
        removed=report.removed,
        hashed=report.hashed,
        ms=report.duration_ms,
    )
    return report


def write_freshness_stat(scope: SessionScope, settings: Settings | None = None) -> FreshnessReport | None:
    """Run the check and record it for the Ops page. Never raises.

    Same rule as ``knowledge/telemetry.record_call``: a status indicator that can crash the worker it
    reports on is worse than no indicator.
    """
    try:
        report = check_freshness(scope, settings)
    except Exception as exc:  # noqa: BLE001 - see above
        logger.warning("freshness.check_failed", error=f"{type(exc).__name__}: {exc}"[:200])
        return None
    try:
        with scope.session() as session:
            MetricsRepo(session).record_service_stat(
                ServiceStat(
                    id=new_id(),
                    at=report.checked_at,
                    service=SERVICE,
                    process_rss_bytes=None,
                    model_loaded=None,
                    model_name=None,
                    details=report.as_details(),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("freshness.record_failed", error=f"{type(exc).__name__}: {exc}"[:200])
    return report
