"""Source-root registry: ``config/source-roots.yaml`` -> :class:`RootContext` (A07a, P6-T03).

This is the only module that reads ``config/source-roots.yaml``. A07b's
:mod:`aimemory.sources.policies` deliberately does not (the dependency direction is one-way), so the
per-root pieces of that file are turned into the shapes A07b already understands here:

* ``exclude_extra`` -> extra ``.memoryignore`` lines (:func:`aimemory.sources.ignore.load_ignore_rules`)
* ``default_policy`` / ``mirror_paths`` -> :class:`aimemory.sources.policies.RootOverride`
* ``priority_paths`` -> the Tier 2 queue order of ADR-0006
* ``origin_overrides`` -> ``sources.origin`` / ``sources.trust`` (AC-6: vault ``Clippings/`` is
  ``origin=external, trust=low``)

Host paths never appear here or in the file itself (ADR-0004): the file carries only a stable
``container_path`` (``/sources/vault``), and the URI is built from ``scheme`` + ``label``. When the
same code runs outside a container (tests, a host-side CLI), :func:`resolve_root_path` accepts an
explicit override so nothing has to hardcode ``D:\\My-Vault``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ..common.config import Settings, get_settings
from ..common.errors import ConfigurationError
from ..domain.enums import Origin, SourceKind, SourceUriScheme, StoragePolicy, Trust
from ..domain.models import SourceRoot
from ..domain.source_uri import build_source_uri
from .ignore import IgnoreRules, load_ignore_rules
from .policies import PoliciesConfig, RootOverride, load_policies_config
from .secrets import SecretDetectorConfig

__all__ = [
    "DEFAULT_PRIORITY",
    "RootContext",
    "build_root_context",
    "load_root_documents",
    "load_source_roots",
    "resolve_root_path",
]

#: Tier 2 queue order (ADR-0006). Lower runs first; ``episodes.priority`` stores the number.
PRIORITY_EXPLICIT = 10  # matched a root's ``priority_paths``
PRIORITY_DECISION = 20  # decision/ADR/architecture documents anywhere in a root
PRIORITY_README = 30  # README / AGENTS.md / CLAUDE.md / index notes
DEFAULT_PRIORITY = 100
PRIORITY_ARCHIVE = 800  # archived material
PRIORITY_EXTERNAL = 900  # ``origin=external`` (vault ``Clippings/``) - always last, AC-6

_DECISION_MARKERS = ("decision", "adr-", "architecture", "rfc-")
_README_MARKERS = ("readme", "agents.md", "claude.md", "index.md", "start here")
_ARCHIVE_MARKERS = ("04 archives/", "archive/", "archives/")

# ``config/source-roots.example.yaml`` documents ``kind: git`` for repository roots, but the frozen
# ``SourceKind`` vocabulary (A02) spells that ``repository``. Accept both spellings in the file.
_KIND_ALIASES = {"git": SourceKind.REPOSITORY, "repo": SourceKind.REPOSITORY}

_KNOWN_KEYS = frozenset(
    {
        "root_id",
        "scheme",
        "label",
        "container_path",
        "device_id",
        "enabled",
        "default_policy",
        "default_project_id",
        "kind",
        "registry_role",
        "priority_paths",
        "mirror_paths",
        "exclude_extra",
        "origin_overrides",
    }
)


def _root_from_mapping(entry: dict[str, Any], *, device_id: str) -> SourceRoot:
    unknown = set(entry) - _KNOWN_KEYS
    if unknown:
        raise ConfigurationError(
            "Unknown key in config/source-roots.yaml.",
            detail=f"root_id={entry.get('root_id')!r} unknown keys: {sorted(unknown)}",
        )
    raw_kind = str(entry.get("kind", "directory")).lower()
    kind = _KIND_ALIASES.get(raw_kind) or SourceKind(raw_kind)
    try:
        return SourceRoot(
            root_id=entry["root_id"],
            scheme=SourceUriScheme(entry["scheme"]),
            label=entry["label"],
            container_path=PurePosixPath(str(entry["container_path"])),
            device_id=entry.get("device_id") or device_id,
            enabled=bool(entry.get("enabled", True)),
            default_policy=StoragePolicy(entry.get("default_policy", "INDEX_CONTENT")),
            default_project_id=entry.get("default_project_id"),
            kind=kind,
            registry_role=entry.get("registry_role"),
            priority_paths=list(entry.get("priority_paths") or []),
            mirror_paths=list(entry.get("mirror_paths") or []),
            exclude_extra=list(entry.get("exclude_extra") or []),
            origin_overrides=[dict(o) for o in (entry.get("origin_overrides") or [])],
        )
    except KeyError as exc:  # pragma: no cover - configuration error path
        raise ConfigurationError(
            "Missing key in config/source-roots.yaml.", detail=f"missing {exc}"
        ) from exc


def load_root_documents(path: Path | None = None, settings: Settings | None = None) -> dict[str, Any]:
    """Raw parsed YAML of the source-root file (``{version, device_id, roots: [...]}``)."""
    resolved = path or (settings or get_settings()).paths.roots_file
    if not Path(resolved).is_file():
        raise ConfigurationError(
            "Source-root registry file not found.",
            detail=(
                f"{resolved} does not exist - copy config/source-roots.example.yaml to "
                "config/source-roots.yaml (it is git-ignored) and adjust it."
            ),
        )
    with open(resolved, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict) or not isinstance(data.get("roots"), list):
        raise ConfigurationError("Malformed source-root registry file.", detail=f"{resolved}")
    return data


def load_source_roots(
    path: Path | None = None,
    *,
    settings: Settings | None = None,
    include_disabled: bool = False,
) -> list[SourceRoot]:
    """Every root declared in ``config/source-roots.yaml`` (enabled ones only, by default)."""
    settings = settings or get_settings()
    data = load_root_documents(path, settings)
    device_id = data.get("device_id") or settings.device_id
    roots = [_root_from_mapping(entry, device_id=device_id) for entry in data["roots"]]
    if include_disabled:
        return roots
    return [root for root in roots if root.enabled]


def resolve_root_path(root: SourceRoot, override: Path | str | None = None) -> Path:
    """Filesystem location of ``root``: explicit override > env override > ``container_path``.

    The env override exists so the CLI and the tests can run outside the container without any host
    path ever entering ``config/source-roots.yaml`` (ADR-0004). Its name is derived from the root id:
    ``vault`` -> ``AIMEMORY_ROOT_PATH_VAULT``, ``joblab-de`` -> ``AIMEMORY_ROOT_PATH_JOBLAB_DE``.
    """
    if override is not None:
        return Path(override)
    env_name = "AIMEMORY_ROOT_PATH_" + root.root_id.upper().replace("-", "_").replace(".", "_")
    from_env = os.environ.get(env_name)
    if from_env:
        return Path(from_env)
    return Path(str(root.container_path))


@dataclass(frozen=True)
class RootContext:
    """One source root, resolved: where it lives, how it is classified, how it is prioritized.

    Built by :func:`build_root_context`. Everything the discovery walk, the policy resolver and the
    Tier 2 queue need about a root is reachable from here, so no other module has to re-read the
    registry file.
    """

    root: SourceRoot
    base_path: Path
    ignore_rules: IgnoreRules
    override: RootOverride
    policies: PoliciesConfig
    secret_config: SecretDetectorConfig
    origin_rules: tuple[tuple[str, Origin, Trust], ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------------------------ identity

    @property
    def root_id(self) -> str:
        return self.root.root_id

    @property
    def is_repository(self) -> bool:
        """True when git metadata (``.git/HEAD``) should be recorded on every version."""
        return self.root.kind is SourceKind.REPOSITORY or self.root.scheme is SourceUriScheme.GIT

    def uri_for(self, relative_path: str) -> str:
        """Logical URI of a path inside this root (ADR-0004)."""
        return build_source_uri(
            self.root.scheme,
            self.root.label,
            relative_path,
            device_id=(
                self.root.device_id if self.root.scheme is SourceUriScheme.LOCALFS else None
            ),
        ).to_string()

    # --------------------------------------------------------------------------- classification

    def is_mirror(self, relative_path: str) -> bool:
        return self.override.is_mirror_path(relative_path) is not None

    def origin_trust(self, relative_path: str) -> tuple[Origin, Trust]:
        """``(origin, trust)`` for a path - AC-6's ``Clippings/`` rule comes from the registry file."""
        rel = relative_path.replace("\\", "/").lstrip("/")
        for prefix, origin, trust in self.origin_rules:
            if rel == prefix or rel.startswith(prefix.rstrip("/") + "/"):
                return origin, trust
        return Origin.INTERNAL, Trust.HIGH

    def priority_for(self, relative_path: str) -> int:
        """Tier 2 queue order (ADR-0006): explicit ``priority_paths`` first, clippings last."""
        rel = relative_path.replace("\\", "/").lstrip("/")
        lower = rel.lower()
        origin, _trust = self.origin_trust(rel)
        if origin is Origin.EXTERNAL:
            return PRIORITY_EXTERNAL
        for prefix in self.root.priority_paths:
            candidate = prefix.replace("\\", "/").lstrip("/")
            if rel == candidate or rel.startswith(candidate.rstrip("/") + "/"):
                return PRIORITY_EXPLICIT
        if any(marker in lower for marker in _ARCHIVE_MARKERS):
            return PRIORITY_ARCHIVE
        name = lower.rsplit("/", 1)[-1]
        if any(marker in name for marker in _DECISION_MARKERS):
            return PRIORITY_DECISION
        if any(name.startswith(marker) or name == marker for marker in _README_MARKERS):
            return PRIORITY_README
        return DEFAULT_PRIORITY


def build_root_context(
    root: SourceRoot,
    *,
    base_path: Path | str | None = None,
    settings: Settings | None = None,
    memoryignore_path: Path | None = None,
    policies_path: Path | None = None,
) -> RootContext:
    """Resolve one :class:`~aimemory.domain.models.SourceRoot` into a :class:`RootContext`.

    ``.memoryignore`` is the project-level file (mounted read-only into the ingestion container);
    a root's own ``.gitignore`` is added on top when present, followed by its ``exclude_extra`` lines.
    """
    settings = settings or get_settings()
    resolved_base = resolve_root_path(root, base_path)
    ignore_file = memoryignore_path or settings.paths.memoryignore_file or _default_memoryignore()
    repo_ignore = resolved_base / ".gitignore"
    ignore_rules = load_ignore_rules(
        ignore_file,
        repo_ignore_path=repo_ignore if repo_ignore.is_file() else None,
        extra_patterns=list(root.exclude_extra),
    )
    policies = load_policies_config(policies_path or settings.paths.policies_file)
    override = RootOverride.from_root_config(
        {
            "default_policy": root.default_policy.value,
            "mirror_paths": list(root.mirror_paths),
            "exclude_extra": list(root.exclude_extra),
        }
    )
    origin_rules = tuple(
        (
            str(rule.get("path", "")).replace("\\", "/").lstrip("/"),
            Origin(rule.get("origin", Origin.INTERNAL.value)),
            Trust(rule.get("trust", Trust.HIGH.value)),
        )
        for rule in root.origin_overrides
        if rule.get("path")
    )
    return RootContext(
        root=root,
        base_path=resolved_base,
        ignore_rules=ignore_rules,
        override=override,
        policies=policies,
        secret_config=policies.secret_detector,
        origin_rules=origin_rules,
    )


def _default_memoryignore() -> Path:
    """``/app/.memoryignore`` in the container, the repository copy otherwise."""
    container = Path("/app/.memoryignore")
    if container.is_file():
        return container
    return Path(__file__).resolve().parents[3] / ".memoryignore"
