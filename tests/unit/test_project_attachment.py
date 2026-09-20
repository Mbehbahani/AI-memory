"""Which project a source gets attached to, and - more importantly - when it gets none.

Why this matters
----------------
``sources.project_id`` is what every per-project answer is built on: coverage, "search only my
JobLab work", "what am I doing for G1". A source with no project is still fully searchable, so a
missing label is a quiet loss of *filtering*, not of content.

A **wrong** label is the expensive failure, and it is worse than no label at all: it removes a note
from one project's answers and inserts it into another's, with nothing on screen to say so. So the
rule this file guards is not "label as much as possible" - it is "label only on evidence, and prefer
nothing to a guess".

Evidence, strongest first:

1. ``project:`` in the note's front matter - the author stating it outright.
2. A project alias matching a folder on the path - where the note happens to live.
3. ``area:`` in the front matter - the PARA grouping, a responsibility rather than a project.
4. The root default.

The ordering of 1 and 2 is the point of this change: before it, a note could only be labelled by the
folder it sat in, so the 135 sources outside a project folder (MEASURED, 2026-09-19) had no way to
declare ownership at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath

import pytest

from aimemory.domain.enums import SourceKind, SourceUriScheme
from aimemory.domain.models import SourceRoot
from aimemory.sources.fingerprint import Fingerprint
from aimemory.sources.pipeline import IngestionPipeline, _frontmatter_project_hints


def _fingerprint(body: str) -> Fingerprint:
    raw = body.encode("utf-8")
    return Fingerprint(
        content_hash="deadbeef", size_bytes=len(raw), mtime=datetime.now(tz=timezone.utc), data=raw
    )


def _root(root_id: str, default_project_id: str | None) -> SourceRoot:
    return SourceRoot(
        root_id=root_id,
        scheme=SourceUriScheme.LOCALFS,
        label=root_id,
        container_path=PurePosixPath(f"/sources/{root_id}"),
        enabled=True,
        kind=SourceKind.DIRECTORY,
        default_project_id=default_project_id,
    )


class _Ctx:
    def __init__(self, root: SourceRoot) -> None:
        self.root = root


@pytest.fixture()
def matcher():
    """A pipeline with a registry already seeded, exercised through ``_project_for`` alone.

    Nothing here touches a database or a disk: the two inputs that decide a project are the alias map
    (produced by the Tier 0 registry) and the path, and both are set directly.
    """
    pipeline = IngestionPipeline.__new__(IngestionPipeline)
    pipeline._alias_map = {
        "joblab lakehouse (de)": "joblab-lakehouse-de",
        "joblab-lakehouse-de": "joblab-lakehouse-de",
        "oploy": "oploy",
        "oploy website": "oploy-website",
    }
    pipeline._project_ids = {"joblab-lakehouse-de", "oploy", "oploy-website"}
    return pipeline


@pytest.fixture()
def ctx():
    """A root with no default project, so every result comes from the path or the front matter."""
    return _Ctx(_root("vault", None))


# ----------------------------------------------------------------- 1. front matter, the new claim


def test_a_note_outside_any_project_folder_can_still_declare_its_project(matcher, ctx):
    """The case this change exists for: an inbox note that says what it belongs to.

    ``00 Inbox/Daily Notes/`` matches no alias, so before front matter was consulted this note was
    unlabelled no matter what it said about itself.
    """
    body = "---\nproject: Oploy Website\n---\n\nnotes about the landing page\n"

    result = matcher._project_for(ctx, "00 Inbox/Daily Notes/2026-09-19.md", _fingerprint(body))

    assert result == "oploy-website"


def test_the_project_id_may_be_written_directly(matcher, ctx):
    """``project: oploy-website`` should work as well as ``project: Oploy Website``."""
    body = "---\nproject: oploy-website\n---\nbody\n"

    assert matcher._project_for(ctx, "03 Resources/x.md", _fingerprint(body)) == "oploy-website"


def test_front_matter_outranks_the_folder(matcher, ctx):
    """A statement beats a coincidence.

    A note filed under Oploy but declaring itself part of the lakehouse belongs to the lakehouse -
    the author said so, and the folder is only where the file was dropped.
    """
    body = "---\nproject: JobLab Lakehouse (DE)\n---\nbody\n"

    result = matcher._project_for(ctx, "01 Projects/Oploy Website/x.md", _fingerprint(body))

    assert result == "joblab-lakehouse-de"


# ----------------------------------------------------------------- 2. the folder rule still holds


def test_the_folder_still_labels_a_note_that_says_nothing(matcher, ctx):
    """The pre-existing behaviour, which the great majority of labelled sources rely on."""
    result = matcher._project_for(
        ctx, "01 Projects/JobLab Lakehouse (DE)/design.md", _fingerprint("no front matter\n")
    )

    assert result == "joblab-lakehouse-de"


def test_the_nearest_folder_wins(matcher, ctx):
    """Path segments are read from the file upwards, so the most specific folder decides."""
    result = matcher._project_for(
        ctx, "Oploy/JobLab Lakehouse (DE)/notes.md", _fingerprint("body")
    )

    assert result == "joblab-lakehouse-de"


# ----------------------------------------------------------------- 3. area, the weakest claim


def test_area_labels_a_note_the_folder_could_not(matcher, ctx):
    """This vault already uses ``area:`` (JobLab, Oploy) and has no ``project:`` key anywhere yet."""
    body = "---\narea: Oploy\ntags: [oploy, website, copywriting]\n---\nbody\n"

    assert matcher._project_for(ctx, "00 Inbox/idea.md", _fingerprint(body)) == "oploy"


def test_the_folder_outranks_area(matcher, ctx):
    """An Area is an ongoing responsibility, not a project.

    A note filed *inside* a project folder has already answered the question, so a broader Area label
    must not override it.
    """
    body = "---\narea: Oploy\n---\nbody\n"

    result = matcher._project_for(ctx, "01 Projects/JobLab Lakehouse (DE)/x.md", _fingerprint(body))

    assert result == "joblab-lakehouse-de"


def test_an_explicit_project_outranks_an_area_in_the_same_note(matcher, ctx):
    body = "---\nproject: JobLab Lakehouse (DE)\narea: Oploy\n---\nbody\n"

    assert matcher._project_for(ctx, "00 Inbox/x.md", _fingerprint(body)) == "joblab-lakehouse-de"


# ----------------------------------------------------------------- 4. refusing to guess


def test_a_project_the_registry_does_not_know_is_ignored_not_invented(matcher, ctx):
    """The registry's authority, enforced here.

    ``sources.project_id`` is a foreign key, so inventing a row would fail the write - but the deeper
    reason is that projects come from ``AIOS/Maps/project-graph.md`` and nowhere else. A note may
    *claim* a project; only the map file may *create* one.
    """
    body = "---\nproject: Some Project I Never Registered\n---\nbody\n"

    assert matcher._project_for(ctx, "00 Inbox/x.md", _fingerprint(body)) is None


def test_tags_never_attach_a_note_to_a_project(matcher, ctx):
    """The mislabelling guard.

    ``tags: [oploy]`` means the note is *about* Oploy, not owned by it. Honouring tags would sweep
    every passing mention into a project and quietly corrupt every per-project answer.
    """
    body = "---\ntags: [oploy, joblab-lakehouse-de]\n---\nbody\n"

    assert matcher._project_for(ctx, "00 Inbox/x.md", _fingerprint(body)) is None


def test_a_malformed_or_unreadable_note_falls_back_rather_than_failing(matcher, ctx):
    """A label is a convenience. Nothing about it may take a scan down."""
    for body in ("---\n: : broken : :\n---\nbody", "---\nunclosed front matter\n", "plain text"):
        assert matcher._project_for(ctx, "00 Inbox/x.md", _fingerprint(body)) is None

    # A large file is hashed by streaming, so its bytes are never held in memory.
    streamed = Fingerprint("h", 99_000_000, datetime.now(tz=timezone.utc), None)
    assert matcher._project_for(ctx, "00 Inbox/x.md", streamed) is None
    assert matcher._project_for(ctx, "00 Inbox/x.md", None) is None


def test_front_matter_is_only_read_for_markdown(matcher, ctx):
    """A ``---`` block at the top of a config file or script is not Obsidian front matter."""
    body = "---\nproject: Oploy\n---\nkey: value\n"

    assert _frontmatter_project_hints("infra/compose.yaml", _fingerprint(body)) == (None, None)
    assert matcher._project_for(ctx, "infra/compose.yaml", _fingerprint(body)) is None


def test_front_matter_beyond_the_window_is_not_hunted_for(matcher, ctx):
    """The block sits at the top by definition; a huge one yields no hint rather than a slow scan."""
    padding = "\n".join(f"filler_{i}: value" for i in range(2000))
    body = f"---\n{padding}\nproject: Oploy\n---\nbody\n"

    assert _frontmatter_project_hints("x.md", _fingerprint(body)) == (None, None)


def test_a_list_valued_key_takes_its_first_entry(matcher, ctx):
    """``sources.project_id`` holds exactly one id, so the first written wins rather than none."""
    body = "---\nproject:\n  - Oploy Website\n  - JobLab Lakehouse (DE)\n---\nbody\n"

    assert matcher._project_for(ctx, "00 Inbox/x.md", _fingerprint(body)) == "oploy-website"


# ----------------------------------------------------------------- 5. the default, unchanged


def test_the_root_default_is_the_last_resort():
    """Used only when nothing else matched, and only if that project actually exists."""
    pipeline = IngestionPipeline.__new__(IngestionPipeline)
    pipeline._alias_map = {}
    pipeline._project_ids = {"joblab-lakehouse-de"}

    known = _Ctx(_root("joblab-de", "joblab-lakehouse-de"))
    assert pipeline._project_for(known, "src/main.py", _fingerprint("code")) == "joblab-lakehouse-de"

    # An unregistered default is dropped rather than written: sources.project_id is a foreign key.
    unknown = _Ctx(_root("joblab-de", "never-registered"))
    assert pipeline._project_for(unknown, "src/main.py", _fingerprint("code")) is None
