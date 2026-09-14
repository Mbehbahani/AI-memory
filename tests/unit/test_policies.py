"""Unit tests for :mod:`aimemory.sources.policies` (P6-T01) - the plan section K resolution order.

Also doubles as the "every adversarial fixture resolves safely" acceptance test: see
``test_adversarial_fixtures_resolve_safely`` at the bottom, which walks
``tests/fixtures/adversarial`` end to end through ignore -> policy -> secret-detector, exactly the
sequence A07a's walker (P6-T03) will use.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from aimemory.domain.enums import StoragePolicy
from aimemory.sources.ignore import load_ignore_rules
from aimemory.sources.policies import (
    PoliciesConfig,
    PolicyDecision,
    RootOverride,
    load_policies_config,
    resolve_policy,
)
from aimemory.sources.secrets import scan

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORYIGNORE = REPO_ROOT / ".memoryignore"
POLICIES_YAML = REPO_ROOT / "config" / "policies.yaml"
ADVERSARIAL_DIR = REPO_ROOT / "tests" / "fixtures" / "adversarial"

sys.path.insert(0, str(ADVERSARIAL_DIR))
from oversized.generate import write_oversized_markdown  # noqa: E402


@pytest.fixture(scope="module")
def cfg() -> PoliciesConfig:
    return load_policies_config(POLICIES_YAML)


@pytest.fixture(scope="module")
def ignore_rules():
    return load_ignore_rules(MEMORYIGNORE)


def _resolve(
    path: str,
    size: int = 100,
    *,
    is_dir: bool = False,
    cfg: PoliciesConfig | None = None,
    **kwargs,
) -> PolicyDecision:
    return resolve_policy(path, size_bytes=size, is_directory=is_dir, config=cfg, **kwargs)


class TestBuiltinDenyList:
    def test_node_modules_is_ignored(self, cfg, ignore_rules) -> None:
        d = _resolve("node_modules/left-pad/index.js", cfg=cfg, ignore_rules=ignore_rules)
        assert d.policy is StoragePolicy.IGNORE
        assert d.rule.startswith("builtin_deny:dir:")

    def test_path_traversal_is_rejected(self, cfg) -> None:
        d = resolve_policy("../../escape.md", size_bytes=10, config=cfg)
        assert d.policy is StoragePolicy.IGNORE
        assert d.rule == "builtin_deny:path_traversal"

    def test_os_junk_file_is_ignored(self, cfg) -> None:
        d = resolve_policy("some/dir/Thumbs.db", size_bytes=10, config=cfg)
        assert d.policy is StoragePolicy.IGNORE
        assert d.rule == "builtin_deny:file:thumbs.db"

    def test_builtin_deny_beats_memoryignore_and_policies_yaml(self, cfg, ignore_rules) -> None:
        # .git would also match nothing useful in policies.yaml (it's a directory with no extension),
        # so this specifically proves the *first* step (not merely "eventually IGNORE") caught it.
        d = _resolve(".git/config", cfg=cfg, ignore_rules=ignore_rules)
        assert d.rule == "builtin_deny:dir:.git"


class TestMemoryignoreStep:
    def test_pyc_ignored_by_memoryignore(self, cfg, ignore_rules) -> None:
        d = _resolve("packages/aimemory/__pycache__/x.pyc", cfg=cfg, ignore_rules=ignore_rules)
        # __pycache__ is caught by the built-in deny list before .memoryignore is even consulted.
        assert d.policy is StoragePolicy.IGNORE

    def test_lockfile_ignored_by_memoryignore(self, cfg, ignore_rules) -> None:
        d = _resolve("package-lock.json", cfg=cfg, ignore_rules=ignore_rules)
        assert d.policy is StoragePolicy.IGNORE
        assert d.rule.startswith("memoryignore:")

    def test_no_ignore_rules_falls_through_to_policies_yaml(self, cfg) -> None:
        d = _resolve("README.md", cfg=cfg, ignore_rules=None)
        assert d.policy is StoragePolicy.INDEX_CONTENT


class TestPoliciesYamlStep:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("notes/plain.md", StoragePolicy.INDEX_CONTENT),
            ("src/app.py", StoragePolicy.INDEX_CONTENT),
            ("infra/schema.sql", StoragePolicy.INDEX_CONTENT),
            ("config/settings.toml", StoragePolicy.INDEX_CONTENT),
            ("notebooks/analysis.ipynb", StoragePolicy.INDEX_CONTENT),
            ("README", StoragePolicy.INDEX_CONTENT),
            ("Dockerfile", StoragePolicy.INDEX_CONTENT),
            ("data/export.csv", StoragePolicy.CATALOG_ONLY),
            ("models/weights.safetensors", StoragePolicy.CATALOG_ONLY),
            ("archive.zip", StoragePolicy.CATALOG_ONLY),
            ("unknown.xyz123", StoragePolicy.CATALOG_ONLY),  # falls through to default_policy
        ],
    )
    def test_extension_and_filename_rules(self, cfg, path: str, expected: StoragePolicy) -> None:
        d = _resolve(path, cfg=cfg)
        assert d.policy is expected

    def test_catalog_only_directory_by_name(self, cfg) -> None:
        d = _resolve("02 Areas/Excalidraw", cfg=cfg, is_dir=True)
        assert d.policy is StoragePolicy.CATALOG_ONLY
        assert d.rule == "policies_yaml:dir:Excalidraw"

    def test_size_limit_downgrades_markdown(self, cfg) -> None:
        d = _resolve("huge.md", size=cfg.limits.max_index_bytes + 1, cfg=cfg)
        assert d.policy is StoragePolicy.CATALOG_ONLY
        assert d.rule.startswith("size_limit:")

    def test_size_limit_downgrades_json_at_the_lower_bound(self, cfg) -> None:
        d = _resolve("big.json", size=cfg.limits.max_json_index_bytes + 1, cfg=cfg)
        assert d.policy is StoragePolicy.CATALOG_ONLY

    def test_small_json_stays_index_content(self, cfg) -> None:
        d = _resolve("small.json", size=100, cfg=cfg)
        assert d.policy is StoragePolicy.INDEX_CONTENT

    def test_oversized_file_generated_at_test_time(self, cfg, tmp_path: Path) -> None:
        path = write_oversized_markdown(tmp_path / "oversized.md")
        size = path.stat().st_size
        assert size > cfg.limits.max_index_bytes
        d = _resolve("oversized.md", size=size, cfg=cfg)
        assert d.policy is StoragePolicy.CATALOG_ONLY
        assert d.rule.startswith("size_limit:")


class TestRootOverride:
    def test_default_policy_override(self, cfg) -> None:
        override = RootOverride(default_policy=StoragePolicy.INDEX_CONTENT)
        d = _resolve("weird.unknownext", cfg=cfg, root_override=override)
        assert d.policy is StoragePolicy.INDEX_CONTENT
        assert d.rule == "default_policy"

    def test_mirror_paths(self, cfg) -> None:
        override = RootOverride(mirror_paths=("AIOS/me.md",))
        d = _resolve("AIOS/me.md", cfg=cfg, root_override=override)
        assert d.policy is StoragePolicy.MIRROR
        assert d.rule == "root_override:mirror"

    def test_exclude_extra(self, cfg) -> None:
        override = RootOverride.from_root_config({"exclude_extra": ["Tableau/"]})
        d = _resolve("Tableau/report.py", cfg=cfg, root_override=override)
        assert d.policy is StoragePolicy.IGNORE
        assert d.rule == "root_override:exclude"

    def test_from_root_config_round_trip(self) -> None:
        override = RootOverride.from_root_config(
            {
                "default_policy": "INDEX_CONTENT",
                "mirror_paths": ["AIOS/me.md"],
                "exclude_extra": ["Tableau/"],
            }
        )
        assert override.default_policy is StoragePolicy.INDEX_CONTENT
        assert override.is_mirror_path("AIOS/me.md") == "AIOS/me.md"
        assert override.exclude_patterns is not None


class TestSecretDetectorDowngrade:
    def test_downgrades_index_content_to_catalog_only(self, cfg) -> None:
        # .env.example is explicitly INDEX_CONTENT (a compound extension in policies.yaml, unignored
        # by the `.memoryignore` negation) precisely so a template file is normally readable; here we
        # simulate someone having pasted a real-looking secret into it anyway.
        result = scan(".env.example", b"AKIAIOSFODNN7EXAMPLE", cfg.secret_detector)
        d = resolve_policy(
            ".env.example",
            size_bytes=10,
            config=cfg,
            secret_result=result,
        )
        assert d.policy is StoragePolicy.CATALOG_ONLY
        assert d.secret_suspected is True
        assert d.rule == "secret_detector"

    def test_never_upgrades_an_ignored_path(self, cfg, ignore_rules) -> None:
        result = scan(".env", b"AKIAIOSFODNN7EXAMPLE", cfg.secret_detector)
        d = _resolve(".env", cfg=cfg, ignore_rules=ignore_rules, secret_result=result)
        assert d.policy is StoragePolicy.IGNORE  # built-in deny/.memoryignore already decided this

    def test_no_suspicion_leaves_decision_untouched(self, cfg) -> None:
        result = scan("notes.md", "just ordinary prose, nothing secret here", cfg.secret_detector)
        d = _resolve("notes.md", cfg=cfg, secret_result=result)
        assert d.policy is StoragePolicy.INDEX_CONTENT
        assert d.secret_suspected is False


# ---------------------------------------------------------------------------------------------------
# Adversarial fixtures -> safe classification (P6-T01 acceptance criterion)
# ---------------------------------------------------------------------------------------------------


def _classify_file(path: Path, *, cfg: PoliciesConfig, ignore_rules) -> PolicyDecision:
    rel = path.relative_to(ADVERSARIAL_DIR).as_posix()
    size = path.stat().st_size
    decision = _resolve(rel, size=size, cfg=cfg, ignore_rules=ignore_rules)
    if decision.policy.stores_text:
        content = path.read_bytes()
        result = scan(rel, content, cfg.secret_detector)
        decision = resolve_policy(
            rel, size_bytes=size, config=cfg, ignore_rules=ignore_rules, secret_result=result
        )
    return decision


def test_adversarial_fixtures_resolve_safely(cfg, ignore_rules) -> None:
    skip_names = {"generate.py", ".gitkeep", "README.md", "__pycache__"}
    files = sorted(
        p
        for p in ADVERSARIAL_DIR.rglob("*")
        if p.is_file() and p.name not in skip_names and p.suffix != ".pyc"
    )
    assert files, "adversarial fixture directory is empty"
    rows: list[tuple[str, str, str]] = []
    for path in files:
        decision = _classify_file(path, cfg=cfg, ignore_rules=ignore_rules)
        rel = path.relative_to(ADVERSARIAL_DIR).as_posix()
        rows.append((rel, decision.policy.value, decision.reason))

        if "fake-secrets" in rel:
            # Both outcomes are "safe": .memoryignore/built-in-deny catching the filename first
            # (IGNORE, content never even read) is strictly stronger than a content-based downgrade
            # (CATALOG_ONLY with secret_suspected=True); either way no text is ever stored.
            assert decision.policy in (StoragePolicy.IGNORE, StoragePolicy.CATALOG_ONLY), rel
            if decision.policy is StoragePolicy.CATALOG_ONLY:
                assert decision.secret_suspected is True, rel
        if rel == "binary-disguised-as-md.md":
            # The policy layer alone does not sniff content (that is the extractor's job); it is
            # INDEX_CONTENT by extension. test_extractors.py proves the extractor itself refuses it.
            assert decision.policy is StoragePolicy.INDEX_CONTENT, rel

    # Printed for humans re-running pytest -s / the report table pasted into the result.
    print("\nfixture -> policy -> reason")
    for rel, policy, reason in rows:
        print(f"{rel} -> {policy} -> {reason}")


def test_path_traversal_string_case_documented_in_readme() -> None:
    readme = (ADVERSARIAL_DIR / "README.md").read_text(encoding="utf-8")
    assert "Path traversal" in readme
    assert "Windows junction" in readme
