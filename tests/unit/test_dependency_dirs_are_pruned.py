"""Directories that hold somebody else's code must never be walked.

The bug this records
--------------------
`BUILTIN_DENY_DIRS` listed exact names: `.venv`, `venv`, `env`, `node_modules`, and so on. A
virtualenv, though, is called whatever the person who made it called it.

MEASURED 2026-09-20, adding this repository as a source root: `.venv-graphiti` contributed **6,518
chunks from 100 files** — more than the entire vault (2,890) and the JobLab repo (1,904) combined —
and a second pass reached 888 files before it was noticed. A sibling `.venv-contracts` was queued
behind it. None of it is authored work; all of it is installed library code.

Library code is the worst possible thing to embed. It is enormous, it is about somebody else's
project, and it crowds the notes the corpus exists to hold. And the failure is quiet: the scan
reports success, the file counts merely look large, and nothing says *why*.

The rule
--------
Match dependency directories by **shape**, not by an exact list, because the list can never be
complete. Authored directories with similar names (`docs`, `packages`, `apps`, `config`) must keep
working — a prefix rule that swallowed `envelope/` or `packages/` would be a worse bug than the one
it fixed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aimemory.sources.ignore import BUILTIN_DENY_DIRS, _is_dependency_dir, load_ignore_rules


@pytest.fixture()
def rules():
    return load_ignore_rules(Path("/app/.memoryignore"))


# ------------------------------------------------------------------ the names that caused the bug


@pytest.mark.parametrize(
    "name",
    [
        ".venv-graphiti",   # MEASURED: 6,518 chunks of library code before anyone looked
        ".venv-contracts",  # its sibling, queued behind it
        ".venv",
        ".venv311",
        "venv-tools",
        "project-venv",
        "site-packages",
        "dist-packages",
        "aimemory.egg-info",
        "numpy-2.1.0.dist-info",
    ],
)
def test_a_dependency_directory_is_recognised_however_it_was_named(name: str) -> None:
    assert _is_dependency_dir(name), f"{name!r} holds installed code and must never be walked"


def test_the_walker_actually_prunes_it(rules) -> None:
    """The unit above is worthless if the walker never consults it."""
    assert rules.should_prune(".venv-graphiti") is True
    assert rules.should_prune("packages/aimemory/sources") is False


# ------------------------------------------------------------------ it must not over-reach


@pytest.mark.parametrize(
    "name",
    ["docs", "packages", "apps", "config", "schemas", "scripts", "reports", "infra", "envelope",
     "environment-notes", "adventure"],
)
def test_authored_directories_are_left_alone(name: str) -> None:
    """A prefix rule that swallowed `envelope/` would be a worse bug than the one it fixed."""
    assert not _is_dependency_dir(name), f"{name!r} is authored work and must still be indexed"


def test_the_exact_list_still_applies_too() -> None:
    """Shape matching is an addition, not a replacement: `node_modules` matches no pattern here."""
    assert "node_modules" in BUILTIN_DENY_DIRS
    assert not _is_dependency_dir("node_modules"), (
        "node_modules is caught by the exact list; if it ever matched by shape, that would mean the "
        "shape rules had grown too loose to trust"
    )
