"""Storage policy resolver (plan section K).

Resolution order, exactly as documented in ``config/policies.yaml``'s header and plan section K::

    built-in deny list -> .memoryignore -> config/policies.yaml -> per-root override
    -> secret-detector downgrade

:func:`resolve_policy` runs all five steps and returns a :class:`PolicyDecision`: a
:class:`~aimemory.domain.enums.StoragePolicy` plus a human-readable ``reason`` that names the exact
rule that matched (stored on the ``sources``/``source_versions`` row and shown on the Ops page) and a
machine-readable ``rule`` token for tests and dashboards to group on.

The secret detector can only ever *downgrade* ``INDEX_CONTENT``/``MIRROR`` to ``CATALOG_ONLY`` - it
never upgrades a decision and it never sees a path that was already going to be ``IGNORE``d, so a
`.env` file inside `.git/` is caught by the built-in deny list, not the detector, and no content is
ever read for it.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

import pathspec
import yaml

from aimemory.common.config import default_config_dir
from aimemory.domain.enums import StoragePolicy

from .ignore import BUILTIN_DENY_DIRS, BUILTIN_DENY_FILES, IgnoreRules
from .secrets import SecretDetectorConfig, SecretScanResult

__all__ = [
    "Limits",
    "PoliciesConfig",
    "PolicyDecision",
    "RootOverride",
    "default_policies_path",
    "load_policies_config",
    "resolve_policy",
]

# extensions treated as "bounded structured text" - plan section K: "yaml/toml/json < 200 KB". The
# config file only names a JSON limit explicitly (``max_json_index_bytes``); the same figure is
# DOCUMENTED here as applying to the sibling structured-text formats named in plan section K, since
# policies.yaml does not (yet) carry a separate number for them.
_BOUNDED_STRUCTURED_EXTENSIONS: frozenset[str] = frozenset(
    {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
)


@dataclass(frozen=True)
class Limits:
    max_index_bytes: int
    max_json_index_bytes: int
    max_text_store_chars: int


@dataclass(frozen=True)
class PoliciesConfig:
    """Compiled ``config/policies.yaml``."""

    version: str
    limits: Limits
    default_policy: StoragePolicy
    index_content_extensions: frozenset[str]
    index_content_filenames: tuple[str, ...]
    catalog_only_extensions: frozenset[str]
    catalog_only_directories: frozenset[str]
    mirror_enabled: bool
    secret_detector: SecretDetectorConfig

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "PoliciesConfig":
        limits_raw = data.get("limits", {})
        index = data.get("index_content", {})
        catalog = data.get("catalog_only", {})
        mirror = data.get("mirror", {})
        return cls(
            version=str(data.get("version", "0.0.0")),
            limits=Limits(
                max_index_bytes=int(limits_raw.get("max_index_bytes", 2_097_152)),
                max_json_index_bytes=int(limits_raw.get("max_json_index_bytes", 204_800)),
                max_text_store_chars=int(limits_raw.get("max_text_store_chars", 2_000_000)),
            ),
            default_policy=StoragePolicy(data.get("default_policy", "CATALOG_ONLY")),
            index_content_extensions=frozenset(
                ext.lower() for ext in index.get("extensions", ())
            ),
            index_content_filenames=tuple(index.get("filenames", ())),
            catalog_only_extensions=frozenset(
                ext.lower() for ext in catalog.get("extensions", ())
            ),
            catalog_only_directories=frozenset(catalog.get("directories", ())),
            mirror_enabled=bool(mirror.get("enabled", False)),
            secret_detector=SecretDetectorConfig.from_mapping(data.get("secret_detector", {})),
        )


def default_policies_path() -> Path:
    return default_config_dir() / "policies.yaml"


@lru_cache(maxsize=8)
def _load_cached(path_str: str, mtime_ns: int) -> PoliciesConfig:
    with open(path_str, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return PoliciesConfig.from_mapping(data)


def load_policies_config(path: Path | None = None) -> PoliciesConfig:
    """Load and cache ``config/policies.yaml``. Re-reads automatically if the file's mtime changes
    (keeps tests that write a temp policies file honest without needing a manual cache-clear)."""
    resolved = path or default_policies_path()
    stat = resolved.stat()
    return _load_cached(str(resolved), stat.st_mtime_ns)


@dataclass(frozen=True)
class RootOverride:
    """Per-root override (``config/source-roots.yaml: roots[].{default_policy,mirror_paths,
    exclude_extra}``). A07a loads that YAML file (git-ignored; :mod:`aimemory.sources.policies` does
    not read it directly, to keep the dependency direction one-way) and builds one of these per root.
    """

    default_policy: StoragePolicy | None = None
    mirror_paths: tuple[str, ...] = ()
    exclude_patterns: pathspec.PathSpec | None = None

    @classmethod
    def from_root_config(cls, root: dict[str, Any]) -> "RootOverride":
        default_policy = root.get("default_policy")
        exclude_extra = root.get("exclude_extra") or []
        return cls(
            default_policy=StoragePolicy(default_policy) if default_policy else None,
            mirror_paths=tuple(root.get("mirror_paths") or ()),
            exclude_patterns=(
                pathspec.PathSpec.from_lines("gitwildmatch", exclude_extra)
                if exclude_extra
                else None
            ),
        )

    def is_mirror_path(self, relative_path: str) -> str | None:
        rel = relative_path.replace("\\", "/").lstrip("/")
        for mirror_path in self.mirror_paths:
            prefix = mirror_path.replace("\\", "/").lstrip("/")
            if rel == prefix or rel.startswith(prefix.rstrip("/") + "/"):
                return mirror_path
        return None


@dataclass(frozen=True)
class PolicyDecision:
    policy: StoragePolicy
    reason: str
    rule: str
    secret_suspected: bool = False


def _suffix(name: str) -> str:
    # PurePosixPath.suffix stops at the first ``.`` from the right; multi-dot names such as
    # ``.env.example`` need special handling done by the caller via filename matching, not here.
    return PurePosixPath(name).suffix.lower()


def _matches_named_filenames(name: str, entries: tuple[str, ...]) -> str | None:
    """``policies.yaml: index_content.filenames`` mixes bare names (``README``, ``Dockerfile``) and
    dotted names (``AGENTS.md``, ``.env.example``). A bare entry matches the file's stem or a
    ``NAME.<anything>`` convention (``README.md``, ``Dockerfile.dev``); a dotted entry matches the
    full basename exactly. Case-sensitive, matching the filesystem conventions these names come from.
    """
    for entry in entries:
        if "." in entry:
            if name == entry:
                return entry
        elif name == entry or name.startswith(entry + "."):
            return entry
    return None


def _matches_extension_suffix(name: str, extensions: frozenset[str]) -> str | None:
    """Match ``name`` against a set of extensions, allowing *compound* extensions such as
    ``.env.example`` (not just the last dot-segment ``_suffix()`` would extract) alongside ordinary
    single extensions like ``.py``. The longest matching entry wins, so a compound entry takes
    priority over a shorter generic one that also happens to match as a plain suffix.
    """
    lower = name.lower()
    best: str | None = None
    for ext in extensions:
        if lower.endswith(ext) and (best is None or len(ext) > len(best)):
            best = ext
    return best


def resolve_policy(
    relative_path: str,
    *,
    size_bytes: int,
    is_directory: bool = False,
    ignore_rules: IgnoreRules | None = None,
    config: PoliciesConfig | None = None,
    root_override: RootOverride | None = None,
    secret_result: SecretScanResult | None = None,
) -> PolicyDecision:
    """Resolve one path to a :class:`PolicyDecision`. Pure function, no file I/O.

    ``secret_result`` should be the output of :func:`aimemory.sources.secrets.scan` when the caller
    has already read (or decided to read) the file's bytes; pass ``None`` when content has not been
    inspected (e.g. the path was going to be ``IGNORE``/``CATALOG_ONLY`` anyway and reading it would
    be wasted work).
    """
    cfg = config or load_policies_config()
    rel = relative_path.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p]
    name = parts[-1] if parts else rel

    # 1. Built-in deny list, part a: path-traversal segments are never valid relative paths. This is
    #    defence in depth alongside A07a's path guard (``PathGuardError``) - a resolver that is only
    #    ever handed a path string, with no filesystem context, still refuses to classify one that
    #    walks outside its root.
    if ".." in parts:
        return PolicyDecision(
            StoragePolicy.IGNORE,
            "built-in deny list: path contains a '..' traversal segment",
            "builtin_deny:path_traversal",
        )

    # 1b. Built-in deny list: directories that are never walked, and OS/editor junk files.
    dir_parts = parts if is_directory else parts[:-1]
    for part in dir_parts:
        if part.lower() in BUILTIN_DENY_DIRS:
            return PolicyDecision(
                StoragePolicy.IGNORE,
                f"built-in deny list: directory '{part}' is never walked",
                f"builtin_deny:dir:{part.lower()}",
            )
    if not is_directory and name.lower() in BUILTIN_DENY_FILES:
        return PolicyDecision(
            StoragePolicy.IGNORE,
            f"built-in deny list: file '{name}' is OS/editor junk",
            f"builtin_deny:file:{name.lower()}",
        )

    # 2. .memoryignore (gitignore semantics, last-match-wins, negation supported).
    if ignore_rules is not None:
        matched = ignore_rules.matching_pattern(rel, is_dir=is_directory)
        if matched is not None:
            return PolicyDecision(
                StoragePolicy.IGNORE,
                f".memoryignore: pattern '{matched}' matched",
                f"memoryignore:{matched}",
            )

    # 3. config/policies.yaml.
    base_default = (
        root_override.default_policy
        if root_override is not None and root_override.default_policy is not None
        else cfg.default_policy
    )
    if is_directory:
        if name in cfg.catalog_only_directories:
            decision = PolicyDecision(
                StoragePolicy.CATALOG_ONLY,
                f"policies.yaml: directory '{name}' is listed under catalog_only.directories",
                f"policies_yaml:dir:{name}",
            )
        else:
            decision = PolicyDecision(
                base_default,
                "policies.yaml: no directory rule matched; using default_policy",
                "default_policy",
            )
    else:
        ext = _suffix(name)
        filename_hit = _matches_named_filenames(name, cfg.index_content_filenames)
        catalog_ext_hit = _matches_extension_suffix(name, cfg.catalog_only_extensions)
        index_ext_hit = _matches_extension_suffix(name, cfg.index_content_extensions)
        if filename_hit is not None:
            decision = PolicyDecision(
                StoragePolicy.INDEX_CONTENT,
                f"policies.yaml: filename '{name}' matches index_content.filenames entry '{filename_hit}'",
                f"policies_yaml:filename:{filename_hit}",
            )
        elif catalog_ext_hit is not None:
            decision = PolicyDecision(
                StoragePolicy.CATALOG_ONLY,
                f"policies.yaml: extension '{catalog_ext_hit}' is listed under catalog_only.extensions",
                f"policies_yaml:ext:{catalog_ext_hit}",
            )
        elif index_ext_hit is not None:
            decision = PolicyDecision(
                StoragePolicy.INDEX_CONTENT,
                f"policies.yaml: extension '{index_ext_hit}' is listed under index_content.extensions",
                f"policies_yaml:ext:{index_ext_hit}",
            )
        else:
            decision = PolicyDecision(
                base_default,
                f"policies.yaml: extension '{ext or '(none)'}' matches no rule; using default_policy",
                "default_policy",
            )

    # 4. Size limits. Only ever downgrades INDEX_CONTENT (MIRROR is opt-in and bypasses size limits by
    #    design - it is a small, deliberately chosen set of files, plan section K).
    if decision.policy is StoragePolicy.INDEX_CONTENT and not is_directory:
        ext = _suffix(name)
        limit = cfg.limits.max_index_bytes
        if ext in _BOUNDED_STRUCTURED_EXTENSIONS:
            limit = min(limit, cfg.limits.max_json_index_bytes)
        if size_bytes > limit:
            decision = PolicyDecision(
                StoragePolicy.CATALOG_ONLY,
                f"size {size_bytes} bytes exceeds the {limit}-byte limit for '{ext or name}'",
                f"size_limit:{limit}",
            )

    # 5. Per-root override: exclude_extra can IGNORE anything above; mirror_paths can MIRROR anything
    #    above (except something the built-in deny list or .memoryignore already refused - those two
    #    steps ran first and already returned).
    if root_override is not None:
        if root_override.exclude_patterns is not None and root_override.exclude_patterns.match_file(
            rel + ("/" if is_directory else "")
        ):
            decision = PolicyDecision(
                StoragePolicy.IGNORE,
                "per-root override: matched a root's exclude_extra pattern",
                "root_override:exclude",
            )
        else:
            mirror_hit = root_override.is_mirror_path(rel)
            if mirror_hit is not None:
                decision = PolicyDecision(
                    StoragePolicy.MIRROR,
                    f"per-root override: '{rel}' is listed under this root's mirror_paths ('{mirror_hit}')",
                    "root_override:mirror",
                )

    # 6. Secret-detector downgrade - can only move INDEX_CONTENT/MIRROR to CATALOG_ONLY, never the
    #    reverse, and never touches an already-IGNORE'd path (no content would have been read for it).
    if secret_result is not None and secret_result.suspected and decision.policy.stores_text:
        decision = PolicyDecision(
            StoragePolicy.CATALOG_ONLY,
            f"secret detector: {secret_result.reason}",
            "secret_detector",
            secret_suspected=True,
        )

    return decision
