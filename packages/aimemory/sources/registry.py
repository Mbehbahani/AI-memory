"""Tier 0: the deterministic project registry seeded from my-vault (A07a, P7-T01, ADR-0006).

No LLM is involved. Two files in the vault are the registry:

* ``AIOS/me.md`` - the *What I'm building* table (``| Project / Domain | Track | What it is | Stage |``)
* ``AIOS/Maps/project-graph.md`` - one table per track heading
  (``| Project | Path | Purpose | Serves | Feeds -> | Consumes <- | Status |``)

Both are ``MIRROR`` paths in ``config/source-roots.yaml``, i.e. the two files the plan allows to be
stored verbatim. They are read here directly through
:func:`aimemory.domain.source_uri.path_guard` because Tier 0 runs before any text extraction.

What this produces: ``projects`` rows (id, name, track, status, goals, summary, attributes) and
``project_aliases`` rows (the display name, the slug, the folder basename from the ``Path`` column),
which is what lets later stages attach a source under ``01 Projects/JobLab Lakehouse (DE)/...`` to the
right project without any model call.

What this deliberately does **not** do: write Neo4j. Wikilinks collected by the markdown extractor
become ``LINKS_TO`` edges in the structural projection, which is A08's ``knowledge/structural.py``
(P7-T03); Postgres stays the system of record (ADR-0001).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from ..common.ids import new_id, normalize_name, slugify
from ..common.logging import get_logger
from ..common.time import utc_now
from ..domain.enums import ProjectStatus, Track
from ..domain.models import Project, ProjectAlias
from ..domain.source_uri import path_guard
from ..persistence.repositories import ProjectRepo
from .roots import RootContext

__all__ = [
    "ME_PATH",
    "PROJECT_GRAPH_PATHS",
    "ParsedProject",
    "parse_me_projects",
    "parse_project_graph",
    "seed_registry",
]

logger = get_logger(__name__)

ME_PATH = "AIOS/me.md"
#: The map lives at ``AIOS/Maps/project-graph.md`` in the real vault and at ``AIOS/project-graph.md``
#: in some older copies; the fixture vault uses the former. Both are tried, in order.
PROJECT_GRAPH_PATHS = ("AIOS/Maps/project-graph.md", "AIOS/project-graph.md")

_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"\*([^*]+?)\*")
_CODE_SPAN = re.compile(r"`([^`]+)`")
_GOAL = re.compile(r"\bG(\d)\b")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")

#: Checked *before* the loose keywords below. One heading in the real vault reads "Research track
#: (keep separate from business content unless explicitly connected)": it names two tracks and only
#: one of them is its own, so the track a heading explicitly *declares* always wins.
_TRACK_PHRASES: tuple[tuple[str, Track], ...] = (
    ("research track", Track.RESEARCH),
    ("career track", Track.CAREER),
    ("build track", Track.BUSINESS),
    ("business track", Track.BUSINESS),
    ("product track", Track.BUSINESS),
    ("foundations", Track.FOUNDATION),
    ("foundation", Track.FOUNDATION),
)

#: Fallback for cells that name a track without the word "track" ("Business / AI Engineering",
#: "Research + product"). ``research`` outranks ``product`` because a row naming both is research
#: work with a product angle, not the other way round.
_TRACK_KEYWORDS: tuple[tuple[str, Track], ...] = (
    ("career", Track.CAREER),
    ("research", Track.RESEARCH),
    ("business", Track.BUSINESS),
    ("product", Track.BUSINESS),
    ("infrastructure", Track.FOUNDATION),
    ("unknown", Track.FOUNDATION),
)

#: Order matters. A terminal or negative state anywhere in the cell beats an activity word, but an
#: explicit "Live"/"Operating" beats a past-tense completion word, because these cells narrate:
#: "Live; blog redesign shipped 2026-08-18" is a live project, not a completed one.
_STATUS_KEYWORDS: tuple[tuple[str, ProjectStatus], ...] = (
    ("abandon", ProjectStatus.ABANDONED),
    ("archive", ProjectStatus.ABANDONED),
    ("park", ProjectStatus.PAUSED),
    ("pause", ProjectStatus.PAUSED),
    ("defer", ProjectStatus.PAUSED),
    ("live", ProjectStatus.ACTIVE),
    ("operating", ProjectStatus.ACTIVE),
    ("complete", ProjectStatus.COMPLETED),
    ("done", ProjectStatus.COMPLETED),
    ("shipped", ProjectStatus.COMPLETED),
    ("plan", ProjectStatus.PLANNED),
    ("template", ProjectStatus.PLANNED),
    ("active", ProjectStatus.ACTIVE),
    ("in progress", ProjectStatus.ACTIVE),
    ("mvp", ProjectStatus.ACTIVE),
    ("ongoing", ProjectStatus.ACTIVE),
    ("pilot", ProjectStatus.ACTIVE),
    ("built", ProjectStatus.ACTIVE),
    ("phase", ProjectStatus.ACTIVE),
)

#: A cell that *opens* by declaring the project unknown is unknown, whatever the rest of the
#: sentence proposes doing about it ("Unknown - confirm or archive" is not an archived project).
_UNKNOWN_PREFIXES = ("unknown", "unclear", "tbd", "to confirm", "?")


def _track_from(text: str, default: Track = Track.FOUNDATION) -> Track:
    lowered = text.lower()
    for phrase, track in _TRACK_PHRASES:
        if phrase in lowered:
            return track
    for keyword, track in _TRACK_KEYWORDS:
        if keyword in lowered:
            return track
    return default


def _status_from(text: str) -> ProjectStatus:
    lowered = _clean_cell(text).lower().strip("*_ ").strip()
    if not lowered or lowered.startswith(_UNKNOWN_PREFIXES):
        return ProjectStatus.UNKNOWN
    for keyword, status in _STATUS_KEYWORDS:
        if keyword in lowered:
            return status
    return ProjectStatus.UNKNOWN


def _clean_cell(cell: str) -> str:
    """Strip markdown emphasis, wikilink syntax and code ticks from one table cell."""
    value = cell.strip()
    value = _WIKILINK.sub(lambda m: m.group(1).split("/")[-1], value)
    value = _BOLD.sub(lambda m: m.group(1), value)
    value = _ITALIC.sub(lambda m: m.group(1), value)
    return value.replace("`", "").strip()


def _cell(header: list[str], cells: list[str], key: str, default: str = "") -> str:
    """Value of the column whose header contains ``key`` (the map uses ``Feeds ->`` etc.)."""
    for index, column in enumerate(header):
        if key in column and index < len(cells):
            return _clean_cell(cells[index])
    return default


def _raw_cell(header: list[str], cells: list[str], key: str, default: str = "") -> str:
    """Same lookup as :func:`_cell`, without cleaning - the markup itself carries meaning.

    Only the ``Path`` column needs it: the backticks are what separate a real filesystem path from
    the prose around it, and :func:`_clean_cell` removes them.
    """
    for index, column in enumerate(header):
        if key in column and index < len(cells):
            return cells[index].strip()
    return default


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    cells = [cell for cell in stripped.strip("|").split("|")]
    return [cell.strip() for cell in cells]


@dataclass
class ParsedProject:
    """One registry row before it becomes a :class:`~aimemory.domain.models.Project`."""

    name: str
    track: Track
    status: ProjectStatus = ProjectStatus.UNKNOWN
    summary: str | None = None
    goal_ids: list[str] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    source_file: str = ""

    @property
    def project_id(self) -> str:
        return slugify(self.name)

    def merge(self, other: ParsedProject) -> None:
        """Merge a second sighting of the same project (``me.md`` and the map both list some)."""
        if other.status is not ProjectStatus.UNKNOWN and self.status is ProjectStatus.UNKNOWN:
            self.status = other.status
        if other.summary and not self.summary:
            self.summary = other.summary
        for goal in other.goal_ids:
            if goal not in self.goal_ids:
                self.goal_ids.append(goal)
        for alias in other.aliases:
            if alias not in self.aliases:
                self.aliases.append(alias)
        for key, value in other.attributes.items():
            self.attributes.setdefault(key, value)


def _path_candidates(path_cell: str) -> list[str]:
    """The filesystem paths named in a ``Path`` cell - and nothing else.

    An alias is matched against *directory names* when a source is attached to a project
    (:meth:`aimemory.sources.pipeline.IngestionPipeline._project_for`), so a junk alias does not just
    add a row - it silently mis-files documents. The real map writes paths as code spans separated by
    ``·`` and wraps commentary around them: ``(KLM, ofi, slides)``, ``· other laptop``,
    ``*no folder, no note, no tracker slot*``. Splitting that on commas produced aliases such as
    ``ofi``, ``slides)`` and ``no tracker slot``. So: take the code spans, and for a cell that has
    none, accept only the pieces that actually contain a path separator.
    """
    spans = list(_CODE_SPAN.findall(path_cell))
    if not spans:
        spans = [piece for piece in path_cell.split("·") if "/" in piece or "\\" in piece]
    candidates: list[str] = []
    for span in spans:
        cleaned = _clean_cell(span).replace("\\", "/").strip().strip("/")
        if cleaned:
            candidates.append(cleaned)
    return candidates


def _aliases_for(name: str, path_cell: str | None = None) -> list[str]:
    aliases = [name]
    slug = None
    try:
        slug = slugify(name)
    except ValueError:  # pragma: no cover - a name with no alphanumerics
        slug = None
    if slug and slug != name:
        aliases.append(slug)
    for candidate in _path_candidates(path_cell or ""):
        basename = candidate.rsplit("/", 1)[-1]
        if basename and basename not in aliases and len(basename) > 1:
            aliases.append(basename)
    return aliases


def parse_me_projects(text: str) -> list[ParsedProject]:
    """Parse the *What I'm building* table of ``AIOS/me.md``."""
    projects: list[ParsedProject] = []
    in_table = False
    header: list[str] = []
    for line in text.splitlines():
        cells = _split_row(line)
        if not cells:
            in_table = False
            header = []
            continue
        if _TABLE_SEPARATOR.match(line):
            continue
        lowered = [c.lower() for c in cells]
        if not in_table:
            if "track" in lowered and any("project" in c or "domain" in c for c in lowered):
                in_table = True
                header = lowered
            continue
        if len(cells) < 2:
            continue
        name = _clean_cell(cells[0])
        if not name or name.lower() in {"project", "project / domain"}:
            continue
        track_cell = cells[header.index("track")] if "track" in header else ""
        stage = _clean_cell(cells[-1]) if len(cells) >= 4 else ""
        summary = _clean_cell(cells[2]) if len(cells) >= 3 else None
        try:
            slugify(name)
        except ValueError:
            continue
        projects.append(
            ParsedProject(
                name=name,
                track=_track_from(track_cell),
                status=_status_from(stage),
                summary=summary,
                attributes={"stage": stage} if stage else {},
                aliases=_aliases_for(name),
                source_file=ME_PATH,
            )
        )
    return projects


def parse_project_graph(text: str) -> list[ParsedProject]:
    """Parse ``AIOS/Maps/project-graph.md``: one table (or bullet list) per track heading.

    Two shapes are supported, because the real vault and the committed test fixture differ:

    * ``| Project | Path | Purpose | Serves | Feeds -> | Consumes <- | Status |`` tables under a
      ``### ... track`` heading (the real vault);
    * ``- [[Project]] - active - ...`` bullets under a ``## Business`` / ``## Research`` heading
      (``tests/fixtures/mini-vault``).
    """
    projects: list[ParsedProject] = []
    current_track = Track.FOUNDATION
    header: list[str] = []
    in_table = False
    in_projects_section = False

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            lowered = heading.lower()
            in_table = False
            header = []
            if lowered.startswith(("projects", "project graph")):
                in_projects_section = True
            elif lowered.startswith(("reverse view", "write-back", "session closeout",
                                     "open questions", "maintenance", "final goals",
                                     "why this note")):
                in_projects_section = False
            else:
                in_projects_section = True
            current_track = _track_from(lowered, default=current_track)
            continue
        if not in_projects_section:
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            content = bullet.group(1)
            link = _WIKILINK.search(content)
            if not link:
                continue
            name = link.group(1).split("/")[-1].strip()
            try:
                slugify(name)
            except ValueError:
                continue
            projects.append(
                ParsedProject(
                    name=name,
                    track=current_track,
                    status=_status_from(content),
                    summary=None,
                    goal_ids=sorted({f"G{m}" for m in _GOAL.findall(content)}),
                    aliases=_aliases_for(name),
                    source_file="project-graph",
                )
            )
            continue

        cells = _split_row(line)
        if not cells:
            in_table = False
            header = []
            continue
        if _TABLE_SEPARATOR.match(line):
            continue
        lowered_cells = [c.lower() for c in cells]
        if not in_table:
            if lowered_cells and lowered_cells[0].startswith("project"):
                in_table = True
                header = lowered_cells
            continue

        name = _clean_cell(cells[0])
        if not name:
            continue
        try:
            slugify(name)
        except ValueError:
            continue

        status_cell = _cell(header, cells, "status") or (
            _clean_cell(cells[-1]) if len(cells) > 1 else ""
        )
        path_cell_raw = _raw_cell(header, cells, "path")
        path_cell = _clean_cell(path_cell_raw)
        serves = _cell(header, cells, "serves")
        attributes: dict[str, str] = {}
        if path_cell:
            attributes["path"] = path_cell[:400]
        feeds = _cell(header, cells, "feeds")
        if feeds:
            attributes["feeds"] = feeds[:400]
        consumes = _cell(header, cells, "consumes")
        if consumes:
            attributes["consumes"] = consumes[:400]
        if status_cell:
            attributes["status_text"] = status_cell[:400]
        projects.append(
            ParsedProject(
                name=name,
                track=current_track,
                status=_status_from(status_cell),
                summary=_cell(header, cells, "purpose") or None,
                goal_ids=sorted({f"G{m}" for m in _GOAL.findall(serves)}),
                attributes=attributes,
                aliases=_aliases_for(name, path_cell_raw),
                source_file="project-graph",
            )
        )
    return projects


def _read_registry_file(ctx: RootContext, relative_path: str) -> str | None:
    """Read one registry file through the path guard. Returns ``None`` when it does not exist."""
    try:
        target = path_guard(ctx.base_path, relative_path)
    except Exception:  # noqa: BLE001 - a guarded path outside the root is simply not read
        return None
    if not Path(target).is_file():
        return None
    return Path(target).read_text(encoding="utf-8", errors="replace")


def collect_registry(ctx: RootContext) -> list[ParsedProject]:
    """Parse both registry files of a bootstrap root and merge them by project id."""
    merged: dict[str, ParsedProject] = {}
    me_text = _read_registry_file(ctx, ME_PATH)
    parsed: list[ParsedProject] = []
    if me_text:
        parsed.extend(parse_me_projects(me_text))
    for candidate in PROJECT_GRAPH_PATHS:
        graph_text = _read_registry_file(ctx, candidate)
        if graph_text:
            parsed.extend(parse_project_graph(graph_text))
            break
    for project in parsed:
        key = project.project_id
        if key in merged:
            merged[key].merge(project)
        else:
            merged[key] = project
    return list(merged.values())


def seed_registry(
    session: Session, ctx: RootContext, *, run_id: UUID | None = None
) -> dict[str, int]:
    """Write the parsed registry into ``projects`` / ``project_aliases``. Idempotent.

    Returns counters (``projects_seeded``, ``aliases_seeded``) that are folded into
    ``ingestion_runs.counters``.
    """
    parsed = collect_registry(ctx)
    repo = ProjectRepo(session)
    now = utc_now()
    projects_written = 0
    aliases_written = 0
    for item in parsed:
        project_id = item.project_id
        existing = repo.get(project_id)
        attributes: dict[str, Any] = dict(item.attributes)
        attributes.setdefault("registry_source", item.source_file or "registry")
        repo.upsert(
            Project(
                id=project_id,
                name=item.name,
                track=item.track,
                parent_id=existing.parent_id if existing else None,
                status=item.status,
                goal_ids=item.goal_ids,
                summary=(item.summary or (existing.summary if existing else None)),
                attributes={k: str(v) for k, v in attributes.items()},
                root_ids=sorted({*(existing.root_ids if existing else []), ctx.root_id}),
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
        )
        projects_written += 1
        for alias in item.aliases:
            normalized = normalize_name(alias)
            if not normalized:
                continue
            repo.add_alias(
                ProjectAlias(
                    id=new_id(),
                    project_id=project_id,
                    alias=alias,
                    normalized_alias=normalized,
                    source="registry",
                )
            )
            aliases_written += 1
    logger.info(
        "ingestion.registry_seeded",
        root=ctx.root_id,
        projects=projects_written,
        aliases=aliases_written,
    )
    return {"projects_seeded": projects_written, "aliases_seeded": aliases_written}
