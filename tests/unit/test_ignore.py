"""Unit tests for :mod:`aimemory.sources.ignore` (P6-T01)."""

from __future__ import annotations

from pathlib import Path

import pytest
from aimemory.sources.ignore import BUILTIN_DENY_DIRS, IgnoreRules, load_ignore_rules

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORYIGNORE = REPO_ROOT / ".memoryignore"


@pytest.fixture()
def repo_rules() -> IgnoreRules:
    return load_ignore_rules(MEMORYIGNORE)


class TestBuiltinDenyDirs:
    @pytest.mark.parametrize(
        "name",
        ["node_modules", ".git", ".venv", "tools-cache", ".obsidian", ".claude", "__pycache__"],
    )
    def test_should_prune_builtin_names(self, name: str, repo_rules: IgnoreRules) -> None:
        assert repo_rules.should_prune(f"some/path/{name}") is True

    def test_should_prune_is_case_insensitive(self, repo_rules: IgnoreRules) -> None:
        assert repo_rules.should_prune("some/NODE_MODULES") is True

    def test_should_not_prune_ordinary_directory(self, repo_rules: IgnoreRules) -> None:
        assert repo_rules.should_prune("01 Projects") is False

    def test_deny_dirs_constant_contains_expected_names(self) -> None:
        for name in ("node_modules", ".git", ".obsidian", ".claude", "tools-cache"):
            assert name in BUILTIN_DENY_DIRS


class TestMemoryignorePatterns:
    def test_pyc_files_ignored(self, repo_rules: IgnoreRules) -> None:
        assert repo_rules.is_ignored("packages/aimemory/__pycache__/foo.pyc") is True

    def test_lockfiles_ignored(self, repo_rules: IgnoreRules) -> None:
        assert repo_rules.is_ignored("package-lock.json") is True
        assert repo_rules.is_ignored("something.lock") is True

    def test_env_ignored_but_env_example_kept(self, repo_rules: IgnoreRules) -> None:
        assert repo_rules.is_ignored(".env") is True
        assert repo_rules.is_ignored(".env.local") is True
        # .memoryignore has an explicit `!.env.example` negation.
        assert repo_rules.is_ignored(".env.example") is False

    def test_matching_pattern_reports_the_rule(self, repo_rules: IgnoreRules) -> None:
        matched = repo_rules.matching_pattern("node_modules/", is_dir=True)
        assert matched == "node_modules/"

    def test_no_match_returns_none(self, repo_rules: IgnoreRules) -> None:
        assert repo_rules.matching_pattern("README.md") is None

    def test_ordinary_markdown_not_ignored(self, repo_rules: IgnoreRules) -> None:
        assert repo_rules.is_ignored("01 Projects/Fixture Project.md") is False


class TestLoadIgnoreRulesComposition:
    def test_missing_files_produce_empty_spec(self, tmp_path: Path) -> None:
        rules = load_ignore_rules(tmp_path / "does-not-exist")
        assert rules.is_ignored("anything.txt") is False

    def test_root_plus_repo_ignore_file_combine(self, tmp_path: Path) -> None:
        root_ignore = tmp_path / ".memoryignore"
        root_ignore.write_text("*.tmp\n", encoding="utf-8")
        repo_ignore = tmp_path / "repo.gitignore"
        repo_ignore.write_text("build/\n!keep.tmp\n", encoding="utf-8")
        rules = load_ignore_rules(root_ignore, repo_ignore_path=repo_ignore)
        assert rules.is_ignored("scratch.tmp") is True
        assert rules.is_ignored("build/", is_dir=True) is True
        # The repo's own file negates a pattern from the root file - last-match-wins, later file wins.
        assert rules.is_ignored("keep.tmp") is False

    def test_extra_patterns_from_root_override(self, tmp_path: Path) -> None:
        root_ignore = tmp_path / ".memoryignore"
        root_ignore.write_text("", encoding="utf-8")
        rules = load_ignore_rules(root_ignore, extra_patterns=["Tableau/"])
        assert rules.should_prune("Tableau") is True

    def test_bom_and_blank_lines_in_ignore_file_are_harmless(self, tmp_path: Path) -> None:
        root_ignore = tmp_path / ".memoryignore"
        root_ignore.write_bytes("﻿# comment\n\n*.secret\n".encode("utf-8"))
        rules = load_ignore_rules(root_ignore)
        assert rules.is_ignored("passwords.secret") is True
