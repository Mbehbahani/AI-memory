"""Warnings for results the system has reason to doubt.

The gap this closes
-------------------
Every hit already carries ``observed_at`` in its provenance, so "this text was read on the 18th" is
technically present in every response. But it sits in metadata beside a confident paragraph of prose,
and a caller - human or model - reads the prose. Nothing in the answer *says* the answer may be old.

Two situations make that dangerous enough to say out loud:

**A disabled root.** ``source_roots.enabled = false`` stops scans and, because
:func:`aimemory.sources.freshness.check_freshness` also skips disabled roots, stops the "files
changed" indicator too. Retrieval, however, has no such check - the chunks are still in ``chunks``
and still rank normally. The result is the worst combination in the system: content frozen at some
past moment, served with full confidence, with the one indicator that would have caught it
deliberately looking away. Nothing is wrong with keeping that data (it cost only local compute, and
it is genuinely useful), so the fix is to say so rather than to hide it.

**A corpus known to have moved.** If the last freshness check found changed files, *some* answer is
out of date; which one is not known here. The honest thing is to report the count, not to guess at
the hit - a warning that names the wrong file is worse than one that names none.

Why a warning and not a filter
------------------------------
Dropping these hits would be the wrong call twice over. A frozen snapshot is still the best available
answer to "what did this repo look like?", and silently returning fewer results is exactly the kind
of invisible degradation :attr:`SearchResult.warnings` exists to prevent (plan section Y: "a degraded
answer the caller cannot see is worse than no answer"). The caller decides; this module makes sure
the caller can.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.logging import get_logger

__all__ = [
    "DISABLED_ROOT_WARNING",
    "STALE_CORPUS_WARNING",
    "staleness_warnings",
]

logger = get_logger(__name__)

#: Mirrors ``aimemory.sources.freshness.SERVICE``. Copied rather than imported: that module pulls in
#: the whole ingestion pipeline (discovery, fingerprinting, extractors), none of which belongs in a
#: read path. ``test_the_freshness_service_name_matches_the_writer`` keeps the two in step.
_FRESHNESS_SERVICE = "freshness"

DISABLED_ROOT_WARNING = (
    "{count} result(s) come from source root '{root_id}', which is disabled - nothing re-reads it, "
    "so this content is frozen as of {observed_at}. Open the file before relying on it."
)

STALE_CORPUS_WARNING = (
    "{count} file(s) on disk have changed since the last scan, so some results may be out of date. "
    "Run a scan to refresh."
)

#: One row per *source*, not per root: the same file can back several hits, and the warning counts
#: hits. ``ANY`` matches a source once however often its id appears, so the tally is done in Python
#: against the ids as they were passed in.
_DISABLED_ROOTS_SQL = """
    SELECT s.id                AS source_id,
           s.root_id           AS root_id,
           sv.observed_at      AS observed_at
      FROM sources s
      JOIN source_roots r  ON r.root_id = s.root_id
 LEFT JOIN source_versions sv ON sv.id = s.current_version_id
     WHERE s.id = ANY(CAST(:source_ids AS uuid[]))
       AND r.enabled IS FALSE
"""

_FRESHNESS_SQL = """
    SELECT details
      FROM service_stats
     WHERE service = :service
     ORDER BY at DESC
     LIMIT 1
"""


def staleness_warnings(session: Session, source_ids: Sequence[Any]) -> list[str]:
    """Warnings about results the system has reason to doubt. Never raises.

    ``source_ids`` are the sources behind the hits actually returned, so a disabled root that
    contributed nothing to *this* answer is not mentioned - a warning about something the caller
    cannot see is noise, and noise is how warnings stop being read.

    Any failure here is swallowed and logged: a search must not fail because the thing that annotates
    it did. The same rule the freshness check and the telemetry writer follow.
    """
    warnings: list[str] = []
    try:
        warnings.extend(_disabled_root_warnings(session, source_ids))
        warnings.extend(_stale_corpus_warnings(session))
    except Exception as exc:  # noqa: BLE001 - see the docstring
        logger.warning("staleness.check_failed", error=f"{type(exc).__name__}: {exc}"[:200])
    return warnings


def _disabled_root_warnings(session: Session, source_ids: Sequence[Any]) -> list[str]:
    if not source_ids:
        return []
    rows = (
        session.execute(_sql(_DISABLED_ROOTS_SQL), {"source_ids": [str(i) for i in source_ids]})
        .mappings()
        .all()
    )
    if not rows:
        return []

    root_by_source = {str(row["source_id"]): row["root_id"] for row in rows}
    newest: dict[str, Any] = {}
    for row in rows:
        seen, root = row["observed_at"], row["root_id"]
        if seen is not None and (newest.get(root) is None or seen > newest[root]):
            newest[root] = seen

    # Counted against the ids as passed in, so two hits from one frozen file read as "2 results".
    hits: dict[str, int] = {}
    for source_id in source_ids:
        root = root_by_source.get(str(source_id))
        if root is not None:
            hits[root] = hits.get(root, 0) + 1

    return [
        DISABLED_ROOT_WARNING.format(
            count=count,
            root_id=root,
            observed_at=(
                newest[root].isoformat() if newest.get(root) is not None else "an unknown time"
            ),
        )
        for root, count in sorted(hits.items())
    ]


def _stale_corpus_warnings(session: Session) -> list[str]:
    """The last freshness reading, if the worker has recorded one and it found work.

    Silent when no check has ever run. "Nothing has changed" and "nobody has looked" are different
    statements, and only the first is worth a warning - the Ops page is where the second belongs,
    because it is a fact about the system rather than about this answer.
    """
    row = session.execute(_sql(_FRESHNESS_SQL), {"service": _FRESHNESS_SERVICE}).first()
    if row is None or not isinstance(row[0], dict):
        return []
    try:
        stale = int(row[0].get("stale_files") or 0)
    except (TypeError, ValueError):
        return []
    return [STALE_CORPUS_WARNING.format(count=stale)] if stale > 0 else []


def _sql(statement: str):
    return text(statement)
