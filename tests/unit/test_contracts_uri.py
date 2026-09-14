"""P1-T01: ADR-0004 source URIs and the plan section T path guard.

Owner: A02. Consumers: A07a (discovery), A09 (source resolution), A13 (security review).
The traversal cases here are the unit-level half of the plan section Y failure test
"traversal via junction"; the integration half belongs to A12.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from aimemory.common.errors import PathGuardError, SourceUriError
from aimemory.domain.enums import SourceUriScheme
from aimemory.domain.source_uri import (
    SourceURI,
    build_source_uri,
    is_safe_relative_path,
    parse_source_uri,
    path_guard,
    safe_relative_path,
)

VALID = [
    "vault://my-vault/AIOS/me.md",
    "vault://my-vault/03 Resources/GitHub/github-repositories-snapshot.json",
    "localfs://local-development-machine/joblab-de/docs/04-source-automation-decision.md",
    "git://jobpilot/src/main.py",
]


@pytest.mark.parametrize("uri", VALID)
def test_parse_build_round_trip(uri: str) -> None:
    parsed = parse_source_uri(uri)
    assert parsed.to_string() == uri
    rebuilt = build_source_uri(
        parsed.scheme, parsed.label, parsed.relative_path, parsed.device_id
    )
    assert rebuilt == parsed
    assert parse_source_uri(rebuilt.to_string()) == parsed


def test_scheme_specific_shapes_match_adr_0004() -> None:
    vault = parse_source_uri("vault://my-vault/AIOS/me.md")
    assert vault.scheme is SourceUriScheme.VAULT
    assert vault.label == "my-vault"
    assert vault.relative_path == "AIOS/me.md"
    assert vault.device_id is None

    localfs = parse_source_uri("localfs://local-development-machine/joblab-de/README.md")
    assert localfs.device_id == "local-development-machine"
    assert localfs.label == "joblab-de"
    assert localfs.relative_path == "README.md"

    repo = parse_source_uri("git://jobpilot/src/main.py")
    assert repo.scheme is SourceUriScheme.GIT and repo.device_id is None


def test_str_is_the_canonical_form() -> None:
    uri = build_source_uri("vault", "my-vault", "AIOS/me.md")
    assert str(uri) == "vault://my-vault/AIOS/me.md"


def test_windows_separators_are_normalized() -> None:
    uri = build_source_uri("localfs", "joblab-de", r"docs\04-decision.md", "local-development-machine")
    assert uri.relative_path == "docs/04-decision.md"
    assert uri.to_string().endswith("/joblab-de/docs/04-decision.md")


def test_leading_dot_slash_is_stripped_but_dotfiles_are_kept() -> None:
    assert build_source_uri("vault", "my-vault", "./AIOS/me.md").relative_path == "AIOS/me.md"
    assert build_source_uri("vault", "my-vault", ".env.example").relative_path == ".env.example"


def test_rename_keeps_the_root() -> None:
    original = parse_source_uri("vault://my-vault/AIOS/me.md")
    moved = original.with_relative_path("AIOS/Maps/me.md")
    assert moved.label == original.label
    assert moved.to_string() == "vault://my-vault/AIOS/Maps/me.md"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "me.md",
        "file:///D:/My-Vault/AIOS/me.md",
        "http://example.com/x",
        "vault://my-vault",
        "vault:///AIOS/me.md",
        "localfs://joblab-de/README.md",
        "vault://My-Vault/AIOS/me.md",
        "vault://my-vault/../../etc/passwd",
        "vault://my-vault//double/slash.md",
        "vault://my-vault/./here.md",
        "vault://my-vault/C:/abs.md",
    ],
)
def test_invalid_uris_are_rejected(bad: str) -> None:
    with pytest.raises(SourceUriError):
        parse_source_uri(bad)


def test_localfs_requires_a_device_id() -> None:
    with pytest.raises(SourceUriError):
        SourceURI(scheme=SourceUriScheme.LOCALFS, label="joblab-de", relative_path="README.md")


def test_non_localfs_rejects_a_device_id() -> None:
    with pytest.raises(SourceUriError):
        SourceURI(
            scheme=SourceUriScheme.VAULT,
            label="my-vault",
            relative_path="a.md",
            device_id="local-development-machine",
        )


@pytest.mark.parametrize(
    "value,expected",
    [
        ("AIOS/me.md", True),
        ("a/b/c.txt", True),
        (".env.example", True),
        ("with space/and (parens).md", True),
        ("", False),
        ("/abs", False),
        ("../escape", False),
        ("a/../b", False),
        ("a//b", False),
        ("a/./b", False),
        ("C:/abs", False),
        ("~/home", False),
        ("back\\slash", False),
        ("nul\x00byte", False),
    ],
)
def test_is_safe_relative_path(value: str, expected: bool) -> None:
    assert is_safe_relative_path(value) is expected


# --------------------------------------------------------------------------------------------------
# path guard
# --------------------------------------------------------------------------------------------------


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    base = tmp_path / "vault"
    (base / "AIOS").mkdir(parents=True)
    (base / "AIOS" / "me.md").write_text("# me", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("nope", encoding="utf-8")
    return base


def test_path_guard_accepts_paths_inside_the_root(root: Path) -> None:
    checked = path_guard(root, "AIOS/me.md", must_exist=True)
    assert checked == (root / "AIOS" / "me.md").resolve()
    assert safe_relative_path(root, checked) == "AIOS/me.md"


def test_path_guard_accepts_an_absolute_path_inside_the_root(root: Path) -> None:
    assert path_guard(root, root / "AIOS" / "me.md").name == "me.md"


@pytest.mark.parametrize(
    "candidate",
    [
        "../outside/secret.txt",
        "AIOS/../../outside/secret.txt",
        "../../..",
        "~/secret",
    ],
)
def test_path_guard_rejects_traversal(root: Path, candidate: str) -> None:
    with pytest.raises(PathGuardError):
        path_guard(root, candidate)


def test_path_guard_rejects_absolute_paths_outside_the_root(root: Path) -> None:
    with pytest.raises(PathGuardError):
        path_guard(root, root.parent / "outside" / "secret.txt")


def test_path_guard_rejects_nul_bytes(root: Path) -> None:
    with pytest.raises(PathGuardError):
        path_guard(root, "AIOS/me\x00.md")


def test_path_guard_reports_missing_files_only_when_asked(root: Path) -> None:
    path_guard(root, "AIOS/not-there.md")  # fine: the caller may be creating a URI
    with pytest.raises(PathGuardError):
        path_guard(root, "AIOS/not-there.md", must_exist=True)


def test_path_guard_error_never_echoes_the_input(root: Path) -> None:
    with pytest.raises(PathGuardError) as excinfo:
        path_guard(root, "../outside/secret.txt")
    assert "secret" not in str(excinfo.value)
    assert "secret" in (excinfo.value.detail or "")


@pytest.mark.skipif(os.name != "nt", reason="junction-style reparse points are Windows-specific")
def test_path_guard_rejects_a_junction_into_the_root(root: Path, tmp_path: Path) -> None:
    """Plan section Y failure test: traversal via junction. The link is refused, not resolved."""
    import subprocess

    link = root / "escape"
    target = tmp_path / "outside"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"mklink /J unavailable: {result.stdout.strip()} {result.stderr.strip()}")
    with pytest.raises(PathGuardError):
        path_guard(root, "escape/secret.txt")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink creation")
def test_path_guard_rejects_a_symlink_into_the_root(root: Path, tmp_path: Path) -> None:
    (root / "escape").symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(PathGuardError):
        path_guard(root, "escape/secret.txt")
