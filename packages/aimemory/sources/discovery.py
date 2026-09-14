"""Discovery walk: one source root -> the files that may be read (A07a, P6-T03, plan section L).

Three rules, all non-negotiable (plan section T, CLAUDE.md):

1. **Directory pruning.** ``IgnoreRules.should_prune()`` (A07b) decides before descending, so
   ``node_modules``, ``.venv``, ``tools-cache``, a bundled JDK, ``.git``, ``.obsidian`` and ``.claude``
   are never walked - not walked-then-filtered. On the pilot repo that is the difference between
   thousands of files and hundreds.
2. **Path guard before every read.** :func:`aimemory.domain.source_uri.path_guard` (A02, frozen) is
   the single definition of "inside the root". This module calls it and never reimplements it.
3. **Symlinks and junctions are not followed.** ``os.scandir`` is used with ``follow_symlinks=False``
   everywhere, link-like entries are skipped outright, and the path guard rejects any path with a
   reparse point on it as a second line of defence.

The walk is read-only: it opens nothing for writing, creates no files, and never touches a root
outside :meth:`RootContext.base_path`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..common.errors import PathGuardError
from ..domain.source_uri import path_guard
from .roots import RootContext

__all__ = ["DiscoveredFile", "SkipReason", "WalkStats", "walk_root"]


class SkipReason:
    """Machine-readable tokens for why a path was not returned by the walk."""

    PRUNED_DIR = "pruned_dir"
    IGNORED_FILE = "ignored_file"
    SYMLINK = "symlink"
    PATH_GUARD = "path_guard"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class DiscoveredFile:
    """One candidate file. No content is read here - only ``stat`` metadata."""

    relative_path: str
    path: Path
    size_bytes: int
    mtime: datetime


@dataclass
class WalkStats:
    """Counters written into ``ingestion_runs.counters`` and shown by ``aimemory-ingest status``."""

    dirs_visited: int = 0
    dirs_pruned: int = 0
    files_seen: int = 0
    files_ignored: int = 0
    symlinks_skipped: int = 0
    path_guard_rejected: int = 0
    unreadable: int = 0

    def as_counters(self) -> dict[str, int]:
        return {
            "dirs_visited": self.dirs_visited,
            "dirs_pruned": self.dirs_pruned,
            "files_seen": self.files_seen,
            "files_ignored": self.files_ignored,
            "symlinks_skipped": self.symlinks_skipped,
            "path_guard_rejected": self.path_guard_rejected,
            "unreadable": self.unreadable,
        }


def _is_link(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(entry.path))


def _mtime(stat_result: os.stat_result) -> datetime:
    return datetime.fromtimestamp(stat_result.st_mtime, tz=UTC)


def walk_root(
    ctx: RootContext,
    *,
    stats: WalkStats | None = None,
    on_skip: Callable[[str, str], None] | None = None,
    subpath: str | None = None,
) -> Iterator[DiscoveredFile]:
    """Yield every readable file under ``ctx.base_path``, deepest-stable order, pruning as it goes.

    ``on_skip(relative_path, reason)`` is called for anything skipped (see :class:`SkipReason`) so a
    caller can log or count it; ``stats`` accumulates the same information.
    """
    stats = stats if stats is not None else WalkStats()
    base = Path(ctx.base_path)
    if not base.is_dir():
        raise PathGuardError(
            "Source root is not a readable directory.", detail=f"missing root: {base}"
        )
    start = base if subpath is None else path_guard(base, subpath)

    def _emit_skip(relative: str, reason: str) -> None:
        if on_skip is not None:
            on_skip(relative, reason)

    stack: list[tuple[Path, str]] = [(start, _relative_of(base, start))]
    while stack:
        directory, rel_dir = stack.pop()
        stats.dirs_visited += 1
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            stats.unreadable += 1
            _emit_skip(rel_dir, SkipReason.UNREADABLE)
            continue
        subdirs: list[tuple[Path, str]] = []
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                if _is_link(entry):
                    stats.symlinks_skipped += 1
                    _emit_skip(rel, SkipReason.SYMLINK)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if ctx.ignore_rules.should_prune(rel):
                        stats.dirs_pruned += 1
                        _emit_skip(rel, SkipReason.PRUNED_DIR)
                        continue
                    subdirs.append((Path(entry.path), rel))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if ctx.ignore_rules.is_ignored(rel):
                    stats.files_ignored += 1
                    _emit_skip(rel, SkipReason.IGNORED_FILE)
                    continue
                guarded = path_guard(base, entry.path)
                stat_result = entry.stat(follow_symlinks=False)
            except PathGuardError:
                stats.path_guard_rejected += 1
                _emit_skip(rel, SkipReason.PATH_GUARD)
                continue
            except OSError:
                stats.unreadable += 1
                _emit_skip(rel, SkipReason.UNREADABLE)
                continue
            stats.files_seen += 1
            yield DiscoveredFile(
                relative_path=rel,
                path=guarded,
                size_bytes=stat_result.st_size,
                mtime=_mtime(stat_result),
            )
        # Reverse so the sorted order is preserved when popping off the stack.
        stack.extend(reversed(subdirs))


def _relative_of(base: Path, target: Path) -> str:
    if base == target:
        return ""
    return target.relative_to(base).as_posix()
