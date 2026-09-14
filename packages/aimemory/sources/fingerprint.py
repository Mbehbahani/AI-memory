"""Fingerprinting: content hash, size, mtime, and git identity (A07a, P6-T03, plan section L).

``source_versions`` is keyed on ``(source_id, content_hash)``, so the content hash - not the mtime -
is what decides whether anything changed. ``sha256`` comes from :mod:`aimemory.common.hashing` with
its ``sha256:`` prefix, so a future algorithm change is visible in the data.

Git metadata for a repository root is read **from the files** ``.git/HEAD``, ``.git/refs/...`` and
``.git/packed-refs``. The ``git`` binary is never invoked: the ingestion container does not have it,
the repository is mounted read-only, and shelling out into a source root is exactly the kind of side
effect plan section T forbids. ``.git`` itself is still pruned by the walker - this is a targeted read
of three well-known text files, containment-checked against the root.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..common.hashing import content_hash_bytes, content_hash_file

__all__ = ["Fingerprint", "GitHead", "fingerprint_file", "read_git_head"]


@dataclass(frozen=True)
class Fingerprint:
    """What one file looked like at scan time.

    ``data`` holds the raw bytes when the file was small enough to read in one go (they are needed
    immediately for the secret scan and the extractor, and re-reading would double the I/O); it is
    ``None`` for files hashed by streaming, which are never text-indexed anyway.
    """

    content_hash: str
    size_bytes: int
    mtime: datetime
    data: bytes | None = None

    @property
    def content_available(self) -> bool:
        return self.data is not None


def fingerprint_file(
    path: Path | str, *, size_bytes: int, mtime: datetime, max_read_bytes: int
) -> Fingerprint:
    """Hash one file. Files up to ``max_read_bytes`` are read once and kept; larger ones are streamed.

    The caller must have passed ``path`` through
    :func:`aimemory.domain.source_uri.path_guard` already (the discovery walk does).
    """
    if size_bytes <= max_read_bytes:
        data = Path(path).read_bytes()
        return Fingerprint(
            content_hash=content_hash_bytes(data),
            size_bytes=len(data),
            mtime=mtime,
            data=data,
        )
    return Fingerprint(
        content_hash=content_hash_file(path), size_bytes=size_bytes, mtime=mtime, data=None
    )


@dataclass(frozen=True)
class GitHead:
    """Checked-out commit and branch of a repository root, read without invoking ``git``."""

    commit: str | None
    branch: str | None

    @property
    def known(self) -> bool:
        return bool(self.commit or self.branch)


def _git_dir(root_path: Path) -> Path | None:
    """``<root>/.git`` as a directory, or the directory a ``gitdir:`` link file points at."""
    candidate = root_path / ".git"
    if candidate.is_dir():
        return candidate
    if candidate.is_file():
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip())
            resolved = target if target.is_absolute() else (root_path / target)
            return resolved if resolved.is_dir() else None
    return None


def _resolve_ref(git_dir: Path, ref: str) -> str | None:
    """Loose ref file first, then ``packed-refs``. Returns the 40-char sha, or ``None``."""
    loose = git_dir / Path(ref)
    try:
        if loose.is_file():
            value = loose.read_text(encoding="utf-8", errors="replace").strip()
            return value or None
    except OSError:
        return None
    packed = git_dir / "packed-refs"
    try:
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(("#", "^")):
                    continue
                parts = stripped.split(None, 1)
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except OSError:
        return None
    return None


def read_git_head(root_path: Path | str) -> GitHead:
    """``(commit, branch)`` of a repository root. Never runs ``git``; never writes anything.

    A detached HEAD yields ``branch=None`` and the raw commit; an unreadable or absent ``.git``
    yields an empty :class:`GitHead` rather than an error (a non-repository root is normal).
    """
    root = Path(root_path)
    git_dir = _git_dir(root)
    if git_dir is None:
        return GitHead(commit=None, branch=None)
    head_file = git_dir / "HEAD"
    try:
        head = head_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return GitHead(commit=None, branch=None)
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        branch = ref.rsplit("/", 1)[-1] if ref else None
        return GitHead(commit=_resolve_ref(git_dir, ref), branch=branch)
    return GitHead(commit=head or None, branch=None)
