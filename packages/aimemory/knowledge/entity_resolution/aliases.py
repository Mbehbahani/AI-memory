"""Deterministic alias tables - the first and cheapest step of entity resolution (A08, P8-T03).

Two tables feed it, and neither involves a model:

* ``config/technology-aliases.yaml: technologies`` - ``PostgreSQL: [postgres, postgresql, pg]``.
  Every spelling normalizes to one canonical ``Technology``.
* ``config/technology-aliases.yaml: projects`` **and** the ``project_aliases`` table A07a seeds from
  the AIOS registry - ``joblab-lakehouse: [JobLab Lakehouse, JobLab DE, ...]``. Every spelling
  normalizes to one ``Project``, scoped by its project slug. This is what stops "JobLab DE" and
  "JobLab Lakehouse (DE)" becoming two nodes.

An alias hit carries a **type**, and that type wins over the model's proposal (ADR-0014 rule 3). This
is measured, not stylistic: P4-T02 found `qwen3:4b` typing ``Claude Code``, ``GitHub Copilot`` and
``OpenClaw`` as ``Project``; the alias table already knows ``Claude Code`` is a ``Technology``.

Lookup is on :func:`aimemory.common.ids.normalize_name` output, so case, accents and trailing
punctuation never produce a miss.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ...common.config import get_settings
from ...common.ids import normalize_name
from ...domain.enums import EntityType

__all__ = ["AliasHit", "AliasIndex", "load_alias_index"]


@dataclass(frozen=True, slots=True)
class AliasHit:
    """One deterministic resolution: what the name really is."""

    canonical_name: str
    entity_type: EntityType
    project_id: str | None = None
    source: str = "config"


class AliasIndex:
    """Normalized alias -> :class:`AliasHit`. Immutable once built; cheap to copy with extras."""

    def __init__(self, entries: Mapping[str, AliasHit] | None = None) -> None:
        self._entries: dict[str, AliasHit] = dict(entries or {})

    # ---- construction ----------------------------------------------------------------------

    @classmethod
    def from_config(cls, path: Path | None = None) -> AliasIndex:
        """Load ``config/technology-aliases.yaml``. Missing file -> an empty index, not a crash."""
        target = path or get_settings().paths.technology_aliases_file
        if not Path(target).exists():
            return cls()
        data = yaml.safe_load(Path(target).read_text(encoding="utf-8")) or {}
        entries: dict[str, AliasHit] = {}

        for canonical, aliases in (data.get("technologies") or {}).items():
            hit = AliasHit(str(canonical), EntityType.TECHNOLOGY, source="technology-aliases.yaml")
            for spelling in [canonical, *(aliases or [])]:
                key = normalize_name(str(spelling))
                if key:
                    entries.setdefault(key, hit)

        for project_id, aliases in (data.get("projects") or {}).items():
            spellings = list(aliases or [])
            display = str(spellings[0]) if spellings else str(project_id)
            hit = AliasHit(
                display, EntityType.PROJECT, project_id=str(project_id), source="technology-aliases.yaml"
            )
            for spelling in [project_id, *spellings]:
                key = normalize_name(str(spelling))
                if key:
                    entries.setdefault(key, hit)

        return cls(entries)

    def with_projects(self, rows: Mapping[str, str]) -> AliasIndex:
        """Overlay the registry's own project names (``project_id -> name``) on the config table.

        The database is more current than the config file, so a registry name replaces the
        config-derived display name while keeping every config spelling pointing at the same slug.
        """
        entries = dict(self._entries)
        for project_id, name in rows.items():
            hit = AliasHit(str(name), EntityType.PROJECT, project_id=str(project_id), source="registry")
            for spelling in (project_id, name):
                key = normalize_name(str(spelling))
                if key:
                    entries[key] = hit
            for key, existing in list(entries.items()):
                if existing.project_id == project_id and existing.entity_type is EntityType.PROJECT:
                    entries[key] = hit
        return AliasIndex(entries)

    def with_project_aliases(self, rows: Mapping[str, str]) -> AliasIndex:
        """Overlay ``project_aliases`` (``normalized_alias -> project_id``) from A07a's Tier 0 seed."""
        entries = dict(self._entries)
        by_project = {
            hit.project_id: hit for hit in entries.values() if hit.entity_type is EntityType.PROJECT
        }
        for normalized, project_id in rows.items():
            known = by_project.get(str(project_id))
            entries[normalize_name(str(normalized))] = AliasHit(
                known.canonical_name if known else str(project_id),
                EntityType.PROJECT,
                project_id=str(project_id),
                source="project_aliases",
            )
        return AliasIndex(entries)

    def extended(self, extra: Mapping[str, AliasHit]) -> AliasIndex:
        return AliasIndex({**self._entries, **{normalize_name(k): v for k, v in extra.items()}})

    # ---- lookup ----------------------------------------------------------------------------

    def lookup(self, name: str) -> AliasHit | None:
        return self._entries.get(normalize_name(name))

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and normalize_name(name) in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> Mapping[str, AliasHit]:
        return dict(self._entries)


@lru_cache(maxsize=1)
def load_alias_index() -> AliasIndex:
    """Process-wide cached config index. Database overlays are applied per resolver, not cached."""
    return AliasIndex.from_config()
