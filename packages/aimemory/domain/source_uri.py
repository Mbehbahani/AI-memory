"""Logical source URIs (ADR-0004) and the filesystem path guard (plan section T).

Consumers: A07a (builds a URI for every discovered file, and calls :func:`path_guard` before every
read), A04 (``sources.uri`` unique index), A08 (provenance stamps), A09 (source resolution -
the Gateway maps a URI back to a mounted path but never returns file bytes), A10 (MCP output),
A12 (``tests/unit/test_contracts_uri.py``, and the plan section Y failure test "traversal via
junction"), A13 (security review).

Grammar (ADR-0004, verbatim)::

    vault://<root-label>/<relative>
    localfs://<device_id>/<root-label>/<relative>
    git://<repo-label>/<relative>

Host paths (``D:\\My-Vault``) appear only in ``.env`` and the Compose mounts; container paths
(``/sources/vault``) only in ``config/source-roots.yaml``. Neither is ever stored in a URI, which is
what makes provenance portable when a root moves.

Ownership note: the ingestion filesystem walk belongs to A07a. :func:`path_guard` lives here as a
*pure contract function* so that the URI layer, the Gateway resolver and the tests all enforce
exactly one definition of "inside the root". A07a calls it; it does not reimplement it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

from pydantic import Field, model_validator

from ..common.errors import PathGuardError, SourceUriError
from .base import DomainModel
from .enums import SourceUriScheme

__all__ = [
    "SourceURI",
    "build_source_uri",
    "is_safe_relative_path",
    "parse_source_uri",
    "path_guard",
    "safe_relative_path",
]

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_URI_RE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<rest>.*)$", re.DOTALL)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")
_RESERVED_SEGMENTS = frozenset({"", ".", ".."})


def _validate_label(label: str, what: str) -> str:
    if not _LABEL_RE.match(label):
        raise SourceUriError(
            f"Invalid {what} in source URI.",
            detail=f"{what}={label!r} must match {_LABEL_RE.pattern}",
        )
    return label


def is_safe_relative_path(value: str) -> bool:
    """True if ``value`` is a POSIX-style relative path with no traversal and no device syntax.

    Rejects: absolute paths, ``..`` and ``.`` segments, empty segments (``a//b``), backslashes,
    Windows drive prefixes, UNC prefixes, ``~`` expansion, control characters and NUL.
    """
    if not value or _CONTROL_RE.search(value):
        return False
    if value.startswith(("/", "\\", "~")) or "\\" in value:
        return False
    if _WINDOWS_DRIVE_RE.match(value):
        return False
    return all(segment not in _RESERVED_SEGMENTS for segment in value.split("/"))


class SourceURI(DomainModel):
    """A parsed logical source URI.

    ``device_id`` is required by, and only meaningful for, the ``localfs`` scheme; ``vault`` and
    ``git`` URIs are device-independent by design (the vault and a git repository can be cloned
    elsewhere without rewriting provenance).
    """

    scheme: SourceUriScheme
    label: str = Field(description="Root label (vault/localfs) or repository label (git)")
    relative_path: str = Field(description="POSIX relative path inside the root")
    device_id: str | None = Field(default=None, description="localfs scheme only")

    @model_validator(mode="after")
    def _check(self) -> SourceURI:
        _validate_label(self.label, "root label")
        if self.scheme is SourceUriScheme.LOCALFS:
            if not self.device_id:
                raise SourceUriError(
                    "localfs URIs require a device id.",
                    detail="ADR-0004: localfs://<device_id>/<root-label>/<relative>",
                )
            _validate_label(self.device_id, "device id")
        elif self.device_id is not None:
            raise SourceUriError(
                "Only localfs URIs carry a device id.",
                detail=f"scheme={self.scheme} device_id={self.device_id!r}",
            )
        if not is_safe_relative_path(self.relative_path):
            raise SourceUriError(
                "Invalid relative path in source URI.",
                detail=f"relative_path={self.relative_path!r}",
            )
        return self

    def __str__(self) -> str:
        return self.to_string()

    def to_string(self) -> str:
        """Render the canonical URI string stored in ``sources.uri``."""
        if self.scheme is SourceUriScheme.LOCALFS:
            return f"{self.scheme.value}://{self.device_id}/{self.label}/{self.relative_path}"
        return f"{self.scheme.value}://{self.label}/{self.relative_path}"

    def with_relative_path(self, relative_path: str) -> SourceURI:
        """Same root, different file - used when a source is renamed inside its root."""
        return self.model_copy(update={"relative_path": relative_path})


def parse_source_uri(value: str) -> SourceURI:
    """Parse a URI string. Raises :class:`~aimemory.common.errors.SourceUriError` on anything else."""
    if not isinstance(value, str) or not value.strip():
        raise SourceUriError("Empty source URI.")
    match = _URI_RE.match(value.strip())
    if not match:
        raise SourceUriError("Source URI is malformed.", detail=f"value={value!r}")
    scheme_text = match.group("scheme")
    try:
        scheme = SourceUriScheme(scheme_text)
    except ValueError as exc:
        allowed = ", ".join(s.value for s in SourceUriScheme)
        raise SourceUriError(
            "Unsupported source URI scheme.", detail=f"scheme={scheme_text!r}; allowed: {allowed}"
        ) from exc

    rest = match.group("rest")
    parts = rest.split("/")
    if scheme is SourceUriScheme.LOCALFS:
        if len(parts) < 3:
            raise SourceUriError(
                "localfs URI needs device, root label and a path.", detail=f"value={value!r}"
            )
        device_id, label, relative = parts[0], parts[1], "/".join(parts[2:])
        return SourceURI(scheme=scheme, label=label, relative_path=relative, device_id=device_id)

    if len(parts) < 2:
        raise SourceUriError("Source URI needs a label and a path.", detail=f"value={value!r}")
    label, relative = parts[0], "/".join(parts[1:])
    return SourceURI(scheme=scheme, label=label, relative_path=relative)


def build_source_uri(
    scheme: SourceUriScheme | str,
    label: str,
    relative_path: str | PurePosixPath,
    device_id: str | None = None,
) -> SourceURI:
    """Build a URI from its parts. ``relative_path`` is normalized to POSIX separators.

    Round-trip guarantee (asserted in ``tests/unit/test_contracts_uri.py``)::

        parse_source_uri(build_source_uri(...).to_string()) == build_source_uri(...)
    """
    normalized = str(relative_path).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.lstrip("/")
    return SourceURI(
        scheme=SourceUriScheme(scheme),
        label=label,
        relative_path=normalized,
        device_id=device_id,
    )


def safe_relative_path(root: Path | str, target: Path | str) -> str:
    """Return the POSIX relative path of ``target`` inside ``root``, after :func:`path_guard`.

    This is the only supported way to turn a filesystem path into the ``relative_path`` of a URI.
    """
    resolved_root = Path(root).resolve(strict=False)
    checked = path_guard(resolved_root, target)
    return checked.relative_to(resolved_root).as_posix()


def path_guard(root: Path | str, candidate: Path | str, *, must_exist: bool = False) -> Path:
    """Canonicalize ``candidate`` and prove it stays inside ``root``; never follow links.

    Plan section T: *"canonicalized paths inside root; symlinks not followed"*. Four checks, in order:

    1. **Syntactic** - no NUL/control characters; a relative candidate is joined to the root, an
       absolute candidate must already be under it.
    2. **Lexical** - ``os.path.normpath`` collapses ``..``/``.`` *before* touching the filesystem, so
       ``../../etc/passwd`` is rejected without a stat call.
    3. **Link** - every component from the root down is checked with ``os.path.islink`` and
       ``os.path.isjunction``. A symlink, junction or other reparse point anywhere on the path is
       refused outright rather than resolved, which is what makes the plan section Y "traversal via
       junction" failure test pass.
    4. **Containment** - the fully resolved path must still be relative to the resolved root, which
       catches anything the earlier checks could not see.

    Raises :class:`~aimemory.common.errors.PathGuardError`. The offending path is put in ``detail``
    only; the public message never echoes caller input.
    """
    root_path = Path(root)
    raw_candidate = str(candidate)
    if _CONTROL_RE.search(raw_candidate) or _CONTROL_RE.search(str(root_path)):
        raise PathGuardError(detail="control character in path")

    resolved_root = root_path.resolve(strict=False)

    candidate_path = Path(raw_candidate)
    if candidate_path.is_absolute() or _WINDOWS_DRIVE_RE.match(raw_candidate):
        joined = candidate_path
    else:
        if raw_candidate.startswith("~"):
            raise PathGuardError(detail=f"home expansion refused: {raw_candidate!r}")
        joined = resolved_root / raw_candidate

    # 2. lexical normalization before any filesystem access
    normalized = Path(os.path.normpath(str(joined)))
    try:
        relative = normalized.relative_to(resolved_root)
    except ValueError as exc:
        raise PathGuardError(detail=f"{raw_candidate!r} escapes root {resolved_root}") from exc

    # 3. reject links/junctions on every component below the root
    walked = resolved_root
    for part in relative.parts:
        walked = walked / part
        if os.path.islink(walked) or (
            hasattr(os.path, "isjunction") and os.path.isjunction(walked)
        ):
            raise PathGuardError(detail=f"link or junction on path: {walked}")

    # 4. containment after full resolution
    final = normalized.resolve(strict=False)
    if final != resolved_root and resolved_root not in final.parents:
        raise PathGuardError(detail=f"{final} is outside {resolved_root}")

    if must_exist and not normalized.exists():
        raise PathGuardError(
            "Path does not exist inside the source root.", detail=f"missing: {normalized}"
        )
    return normalized
