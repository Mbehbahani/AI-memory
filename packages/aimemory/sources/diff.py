"""Change detection - the seven cases of plan section L (A07a, P6-T03).

Pure functions: this module never touches the database or the filesystem. The caller hands it what it
found on disk (:class:`Observed`) and what the database already knows (:class:`KnownSource`), and gets
back one :class:`ChangeDecision` per source, including the deletions the walk could not see.

The table this implements, verbatim from plan section L:

===========  ==========================================  ====================================
Case         Detection                                   Action (taken by the pipeline)
===========  ==========================================  ====================================
unchanged    same hash, same URI                         touch ``last_seen_at``; no reprocessing
modified     same URI, new hash                          new version; re-chunk; embeddings reused
                                                         by ``text_hash``; ``document_change``
                                                         episode; old facts ``unconfirmed``
moved        hash seen at A (now missing) appears at B   same ``source_id``, URI updated, event
                                                         recorded; no re-extraction
deleted      URI missing after a full root scan          ``status=deleted``; knowledge kept, flagged
duplicate    same hash at two URIs                       text/chunks once per hash; episodes once;
                                                         both sources linked
===========  ==========================================  ====================================

Two details that are easy to get wrong and are therefore decided here, once:

* **moved vs duplicate** is decided by whether the *other* URI carrying that hash still exists in
  this scan. Gone -> the file was renamed (``moved``). Still there -> the content now exists twice
  (``duplicate``). This is why deletion detection and move detection must both run after the walk has
  finished, not while it is streaming.
* **a deleted source whose URI comes back** is a ``restored`` flag on top of the normal
  unchanged/modified decision, never a second ``sources`` row - ADR-0005 rule 4 keeps knowledge, and
  a new row would orphan it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from ..domain.enums import ChangeType, SourceStatus

__all__ = ["ChangeDecision", "KnownSource", "Observed", "classify_changes", "summarize"]


@dataclass(frozen=True)
class Observed:
    """A file as the walk + fingerprint saw it."""

    uri: str
    relative_path: str
    content_hash: str
    size_bytes: int
    mtime: datetime


@dataclass(frozen=True)
class KnownSource:
    """A ``sources`` row (plus its current version's hash) as the database already knows it."""

    source_id: UUID
    uri: str
    relative_path: str
    content_hash: str | None
    status: SourceStatus = SourceStatus.ACTIVE
    version_id: UUID | None = None


@dataclass(frozen=True)
class ChangeDecision:
    """What the pipeline must do with one source this scan."""

    change_type: ChangeType
    uri: str
    relative_path: str
    observed: Observed | None = None
    known: KnownSource | None = None
    #: For ``moved``: the URI the content used to live at. For ``duplicate``: the URI that already
    #: holds the text and chunks for this content hash.
    counterpart_uri: str | None = None
    counterpart_source_id: UUID | None = None
    #: The source was ``deleted`` and has come back at the same URI (``source_events.restored``).
    restored: bool = False
    reason: str = ""

    @property
    def needs_new_version(self) -> bool:
        """``new``/``modified``/``duplicate`` create a ``source_versions`` row; the rest do not."""
        return self.change_type in (ChangeType.NEW, ChangeType.MODIFIED, ChangeType.DUPLICATE)

    @property
    def needs_content_processing(self) -> bool:
        """Only ``new`` and ``modified`` extract/chunk/embed. A ``duplicate`` reuses the original's
        text and chunks (plan section L: "text/chunks once per hash"); a ``moved`` file keeps
        everything it already had."""
        return self.change_type in (ChangeType.NEW, ChangeType.MODIFIED)


def classify_changes(
    observed: Iterable[Observed],
    known: Sequence[KnownSource],
    *,
    full_scan: bool = True,
) -> list[ChangeDecision]:
    """Classify one scan of one root.

    ``full_scan=False`` (a partial/sub-path scan) suppresses deletion detection: a URI that was not
    walked this time says nothing about whether the file still exists.
    """
    observed_list = list(observed)
    by_uri: dict[str, Observed] = {item.uri: item for item in observed_list}
    known_by_uri: dict[str, KnownSource] = {row.uri: row for row in known}
    known_by_hash: dict[str, list[KnownSource]] = {}
    for row in known:
        if row.content_hash:
            known_by_hash.setdefault(row.content_hash, []).append(row)

    decisions: list[ChangeDecision] = []
    claimed_move_sources: set[UUID] = set()

    for item in observed_list:
        existing = known_by_uri.get(item.uri)
        if existing is not None:
            restored = existing.status is SourceStatus.DELETED
            if existing.content_hash == item.content_hash:
                decisions.append(
                    ChangeDecision(
                        change_type=ChangeType.UNCHANGED,
                        uri=item.uri,
                        relative_path=item.relative_path,
                        observed=item,
                        known=existing,
                        restored=restored,
                        reason="same hash at the same URI",
                    )
                )
            else:
                decisions.append(
                    ChangeDecision(
                        change_type=ChangeType.MODIFIED,
                        uri=item.uri,
                        relative_path=item.relative_path,
                        observed=item,
                        known=existing,
                        restored=restored,
                        reason="same URI, new content hash",
                    )
                )
            continue

        # New URI. Does this content already live somewhere else?
        candidates = [
            row
            for row in known_by_hash.get(item.content_hash, ())
            if row.uri not in by_uri  # the old URI is gone from this scan -> a rename
            and row.source_id not in claimed_move_sources
            and row.status is not SourceStatus.DELETED
        ]
        if candidates:
            origin = candidates[0]
            claimed_move_sources.add(origin.source_id)
            decisions.append(
                ChangeDecision(
                    change_type=ChangeType.MOVED,
                    uri=item.uri,
                    relative_path=item.relative_path,
                    observed=item,
                    known=origin,
                    counterpart_uri=origin.uri,
                    counterpart_source_id=origin.source_id,
                    reason="content hash reappeared at a new URI while the old URI vanished",
                )
            )
            continue

        twins = [
            row
            for row in known_by_hash.get(item.content_hash, ())
            if row.uri in by_uri and row.status is not SourceStatus.DELETED
        ]
        if twins:
            twin = twins[0]
            decisions.append(
                ChangeDecision(
                    change_type=ChangeType.DUPLICATE,
                    uri=item.uri,
                    relative_path=item.relative_path,
                    observed=item,
                    counterpart_uri=twin.uri,
                    counterpart_source_id=twin.source_id,
                    reason="identical content already stored under another URI in this root",
                )
            )
            continue

        # Two new files with identical content in the same scan: the first is `new`, the rest are
        # duplicates of it (same rule, applied within the batch instead of against the database).
        earlier = next(
            (
                d
                for d in decisions
                if d.observed is not None
                and d.observed.content_hash == item.content_hash
                and d.change_type in (ChangeType.NEW, ChangeType.MODIFIED)
            ),
            None,
        )
        if earlier is not None:
            decisions.append(
                ChangeDecision(
                    change_type=ChangeType.DUPLICATE,
                    uri=item.uri,
                    relative_path=item.relative_path,
                    observed=item,
                    counterpart_uri=earlier.uri,
                    counterpart_source_id=earlier.known.source_id if earlier.known else None,
                    reason="identical content to another file discovered in the same scan",
                )
            )
            continue

        decisions.append(
            ChangeDecision(
                change_type=ChangeType.NEW,
                uri=item.uri,
                relative_path=item.relative_path,
                observed=item,
                reason="URI and content hash both unknown",
            )
        )

    if full_scan:
        moved_away = {
            decision.counterpart_uri
            for decision in decisions
            if decision.change_type is ChangeType.MOVED
        }
        for row in known:
            if row.uri in by_uri or row.uri in moved_away:
                continue
            if row.status is SourceStatus.DELETED:
                continue
            decisions.append(
                ChangeDecision(
                    change_type=ChangeType.DELETED,
                    uri=row.uri,
                    relative_path=row.relative_path,
                    known=row,
                    reason="URI missing after a full root scan",
                )
            )
    return decisions


def summarize(decisions: Iterable[ChangeDecision]) -> dict[str, int]:
    """``{'new': 3, 'unchanged': 120, ...}`` - written into ``ingestion_runs.counters``."""
    counts: dict[str, int] = {change.value: 0 for change in ChangeType}
    for decision in decisions:
        counts[decision.change_type.value] += 1
    return counts


@dataclass
class ScanPlan:
    """Convenience container returned by the pipeline's diff stage."""

    decisions: list[ChangeDecision] = field(default_factory=list)
    counters: Mapping[str, int] = field(default_factory=dict)
