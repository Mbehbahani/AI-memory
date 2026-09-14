"""``.memoryignore`` handling (plan section K) via ``pathspec`` (gitignore semantics).

Resolution order (plan section K / ``config/policies.yaml`` header comment)::

    built-in deny list -> .memoryignore -> config/policies.yaml -> per-root override
    -> secret-detector downgrade

This module owns the first two steps. :mod:`aimemory.sources.policies` calls it before consulting
``config/policies.yaml``.

Two layers of ignore rules exist:

* **Built-in deny list** (:data:`BUILTIN_DENY_DIRS` / :data:`BUILTIN_DENY_FILES`) - directory and file
  names that are *never* walked or indexed, regardless of any config file. This is a hard safety net:
  even a misconfigured or missing ``.memoryignore`` cannot make the walker descend into
  ``node_modules``, ``.git``, ``.venv``, a bundled JDK cache, or vault/editor internals.
* **``.memoryignore`` patterns** - gitignore syntax, compiled with :mod:`pathspec`. Two files are
  combined into one flat pattern list, in this order: the AI Memory repository's own root-level
  ``.memoryignore`` (applied to *every* source root per its header comment), then an optional
  root-specific ignore file (for example the source repository's own ``.gitignore``, when the root
  being scanned is itself a git repository such as ``joblab-de``). Concatenating in that order and
  compiling into a single :class:`pathspec.PathSpec` reproduces gitignore's "last matching pattern
  wins" rule so a more specific, later pattern (including a ``!negation``) overrides an earlier,
  more general one - the same effect real git gets from layered ``.gitignore`` files, flattened here
  because ``pathspec`` has no notion of a directory-scoped ignore file.

Directory pruning (:func:`IgnoreRules.should_prune`) is exposed for A07a's discovery walker so a
matched directory is never descended into (not just filtered out after a full recursive walk, which
would be wasteful and would still touch files a human never wants read, e.g. inside ``.ssh/``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pathspec

__all__ = [
    "BUILTIN_DENY_DIRS",
    "BUILTIN_DENY_FILES",
    "IgnoreRules",
    "load_ignore_rules",
]

# Lower-cased directory names that are always pruned. Comparison is case-insensitive because the
# vault and project roots are read from a Windows filesystem (case-preserving, case-insensitive).
BUILTIN_DENY_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".obsidian",
        ".claude",
        ".venv",
        "venv",
        "env",
        ".env",  # a directory named literally ".env" is as dangerous to walk as the secret file
        "tools-cache",  # also where the JobLab DE bundled JDK lives (plan section C)
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".tox",
        ".idea",
        ".vscode",
        ".next",
        ".svelte-kit",
        ".cache",
        "$recycle.bin",
        "system volume information",
    }
)

BUILTIN_DENY_FILES: frozenset[str] = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})


def _read_lines(path: Path | None) -> list[str]:
    """Read a gitignore-style file. Missing files contribute no patterns (not an error)."""
    if path is None or not path.is_file():
        return []
    # utf-8-sig strips a BOM if present without failing on files that have none.
    return path.read_text(encoding="utf-8-sig").splitlines()


@dataclass(frozen=True)
class IgnoreRules:
    """Compiled ``.memoryignore`` rules for one source root."""

    spec: pathspec.PathSpec

    @staticmethod
    def _normalize(relative_path: str, *, is_dir: bool) -> str:
        rel = relative_path.replace("\\", "/").lstrip("/")
        if is_dir and rel and not rel.endswith("/"):
            rel += "/"
        return rel

    def matching_pattern(self, relative_path: str, *, is_dir: bool = False) -> str | None:
        """The raw pattern line of the *last* pattern that matches ``relative_path``, or ``None``.

        Mirrors :meth:`pathspec.PathSpec.match_file` (last match wins, a ``!negated`` pattern can
        un-ignore) but also returns which line matched, so :mod:`aimemory.sources.policies` can build
        a reason string that names the exact rule. ``pattern.pattern`` is the raw source line that
        ``pathspec`` keeps on every compiled pattern object (comment/blank lines compile to a no-op
        with ``regex is None`` and are skipped here).
        """
        probe = self._normalize(relative_path, is_dir=is_dir)
        if not probe:
            return None
        last_hit: str | None = None
        for pattern in self.spec.patterns:
            if pattern.regex is None:  # blank/comment/whitespace-only line - not a real rule
                continue
            if pattern.match_file(probe) is not None:
                last_hit = pattern.pattern if pattern.include else None
        return last_hit

    def is_ignored(self, relative_path: str, *, is_dir: bool = False) -> bool:
        return self.matching_pattern(relative_path, is_dir=is_dir) is not None

    def should_prune(self, relative_dir: str) -> bool:
        """True when A07a's walker must not descend into ``relative_dir``.

        Checks the built-in deny list first (cheap, no regex) and then the compiled
        ``.memoryignore``/``.gitignore`` patterns as a directory match.
        """
        rel = relative_dir.replace("\\", "/").strip("/")
        if not rel:
            return False
        name = rel.rsplit("/", 1)[-1]
        if name.lower() in BUILTIN_DENY_DIRS:
            return True
        return self.is_ignored(rel, is_dir=True)


def load_ignore_rules(
    memoryignore_path: Path,
    *,
    repo_ignore_path: Path | None = None,
    extra_patterns: list[str] | None = None,
) -> IgnoreRules:
    """Build :class:`IgnoreRules` from the project's root-level ``.memoryignore`` plus, optionally,
    the scanned root's own ignore file (e.g. its ``.gitignore``) and any per-root ``exclude_extra``
    lines from ``config/source-roots.yaml`` (A07a passes those in; this module does not read that
    file, to keep the dependency direction one-way).
    """
    lines = [*_read_lines(memoryignore_path), *_read_lines(repo_ignore_path), *(extra_patterns or [])]
    spec = pathspec.PathSpec.from_lines("gitwildmatch", lines)
    return IgnoreRules(spec=spec)
