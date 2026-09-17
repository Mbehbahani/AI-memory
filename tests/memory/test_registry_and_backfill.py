"""Tier 0 registry parsing and the Tier 1 backfill (P7-T01 / P7-T02, owner A07a).

Two things are covered here that the change-detection scenarios do not touch.

**Tier 0 parsing (P7-T01).** ``aimemory.sources.registry`` reads two real, hand-written Obsidian
notes. Every table fixture below is a verbatim-shaped excerpt of the real ``D:\\My-Vault`` files (the
vault itself is never read by a test - ``tests/conftest.py`` forbids it), reduced to the rows that
previously parsed *wrongly*:

* a ``### Research track (keep separate from business content ...)`` heading that mentions two
  tracks - the loose keyword scan matched ``business`` first and filed three research projects under
  the business track;
* a ``Path`` column whose commentary (``(KLM, ofi, slides)``) was split on commas into aliases like
  ``ofi`` and ``slides)``, which are matched against *directory names* when a source is attached to
  a project and therefore mis-file documents;
* a status cell reading ``Live; blog redesign shipped 2026-08-18`` (a live project read as
  completed) and one reading ``Unknown - confirm or archive`` (an unknown project read as
  abandoned).

**Tier 1 backfill (P7-T02).** The obvious operator sequence - catalogue the vault with ``--tier 0``,
then embed it with ``--tier 1`` - used to produce nothing at all, because the second scan sees every
file as ``unchanged`` and only *changed* versions ever reached the content stages. That is asserted
end to end against Postgres here, together with the invariant that no chunk is ever handed to the
embedding service above its 8 000-character limit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from aimemory.domain.enums import ProjectStatus, Track
from aimemory.sources.pipeline import EMBED_MAX_CHARS, _bounded_drafts
from aimemory.sources.registry import parse_me_projects, parse_project_graph

pytestmark = pytest.mark.usefixtures("postgres_available")


# ==================================================================================================
# Tier 0: parsing (no database, no vault)
# ==================================================================================================

ME_TABLE = """# me.md

## What I'm building

| Project / Domain | Track | What it is | Stage |
|---|---|---|---|
| **Enterprise RAG / AI Engineering** | Business | Production LLM systems | Active |
| **Personal Harness** | Business / AI Engineering | Local MCP control plane | Pilot-ready local implementation; **parked 2026-09-12** - kept as portfolio evidence |
| **HMM-REM / statistical modeling** | Research | Hidden Markov modelling | Active; not validated |
| **Optimization / OR systems** | Research + product | Routing and scheduling models | Active |

## Terms & Shorthand

| Term | Meaning |
|---|---|
| RAG | Retrieval-Augmented Generation |
"""

GRAPH_TABLE = """# project-graph

## Why this note exists

Prose that mentions a business project and a research project but is not a table.

## Projects

### Build track (business / product)

| Project | Path | Purpose | Serves | Feeds -> | Consumes <- | Status |
|---|---|---|---|---|---|---|
| **JobLab Lakehouse (DE)** | `D:\\AWS2\\SupaBaseProject\\DE` | Medallion pipeline | G1 | Career Evidence | Data Pipeline | Phase 4/5 in progress |
| **Oploy Website** | `D:\\AWS2\\Wagtail` | oploy.eu | G3 | Content Center | Home Infrastructure | Live; blog redesign shipped 2026-08-18 |

### Career track

| Project | Path | Purpose | Serves | Feeds -> | Consumes <- | Status |
|---|---|---|---|---|---|---|
| **Interview prep** | `D:\\Apply files\\interview` (KLM, ofi, slides) | Rehearsal decks | G1 | Career Evidence | project facts | Per-application |

### Research track (keep separate from business content unless explicitly connected)

| Project | Path | Purpose | Serves | Feeds -> | Consumes <- | Status |
|---|---|---|---|---|---|---|
| **Stateful REM on Databricks** | `D:\\Azure-DataBricks\\PHD-DS` | Python re-expression | **G2** | PhD vault | Databricks | Demo built; **not validated** |

### Foundations and unknowns

| Project | Path | Purpose | Serves | Feeds -> | Consumes <- | Status |
|---|---|---|---|---|---|---|
| **ReL** | `D:\\ReL` (ChatExport, site, tools) | *Unknown* | ? | ? | - | **Unknown - confirm or archive** |
| **Dutch** | *no folder, no note, no tracker slot* | - | **G4** | - | - | **Gap: a stated goal with zero infrastructure** |

## Reverse view: what serves each goal

| Goal | Projects pulling toward it | Reading |
|---|---|---|
| **G1** | Lakehouse, Interview prep | Not a project table. |
"""


def _by_id(rows):
    return {row.project_id: row for row in rows}


def test_me_table_rows_become_projects_with_their_declared_track() -> None:
    projects = _by_id(parse_me_projects(ME_TABLE))

    # The "Terms & Shorthand" table has no Track column and must not contribute rows.
    assert set(projects) == {
        "enterprise-rag-ai-engineering",
        "personal-harness",
        "hmm-rem-statistical-modeling",
        "optimization-or-systems",
    }
    assert projects["enterprise-rag-ai-engineering"].track is Track.BUSINESS
    assert projects["hmm-rem-statistical-modeling"].track is Track.RESEARCH
    # "Research + product": research work with a product angle, not a business project.
    assert projects["optimization-or-systems"].track is Track.RESEARCH
    # "parked" beats the "Pilot-ready" that opens the same cell.
    assert projects["personal-harness"].status is ProjectStatus.PAUSED


def test_research_track_heading_that_mentions_business_still_files_under_research() -> None:
    projects = _by_id(parse_project_graph(GRAPH_TABLE))

    assert projects["stateful-rem-on-databricks"].track is Track.RESEARCH
    assert projects["joblab-lakehouse-de"].track is Track.BUSINESS
    assert projects["interview-prep"].track is Track.CAREER
    assert projects["rel"].track is Track.FOUNDATION


def test_project_graph_ignores_non_project_tables_and_prose() -> None:
    projects = _by_id(parse_project_graph(GRAPH_TABLE))

    assert set(projects) == {
        "joblab-lakehouse-de",
        "oploy-website",
        "interview-prep",
        "stateful-rem-on-databricks",
        "rel",
        "dutch",
    }


def test_path_column_yields_folder_aliases_and_no_commentary() -> None:
    projects = _by_id(parse_project_graph(GRAPH_TABLE))

    # The code span is the path; "(KLM, ofi, slides)" is commentary and must not become an alias,
    # because aliases are matched against directory names when a source is attached to a project.
    assert projects["interview-prep"].aliases == ["Interview prep", "interview-prep", "interview"]
    assert projects["joblab-lakehouse-de"].aliases[-1] == "DE"
    assert projects["rel"].aliases == ["ReL", "rel"]
    # A cell with no path at all contributes nothing beyond the name and its slug.
    assert projects["dutch"].aliases == ["Dutch", "dutch"]


def test_status_cells_are_read_as_their_headline_state() -> None:
    projects = _by_id(parse_project_graph(GRAPH_TABLE))

    assert projects["oploy-website"].status is ProjectStatus.ACTIVE  # "Live; ... shipped ..."
    assert projects["rel"].status is ProjectStatus.UNKNOWN  # "Unknown - confirm or archive"
    assert projects["dutch"].status is ProjectStatus.UNKNOWN
    assert projects["joblab-lakehouse-de"].status is ProjectStatus.ACTIVE
    assert projects["stateful-rem-on-databricks"].status is ProjectStatus.ACTIVE


# ==================================================================================================
# Tier 1: the chunk-size invariant
# ==================================================================================================


def test_oversized_draft_is_split_below_the_embedding_service_limit() -> None:
    """The pipeline must never hand the embedding service a text it answers 422 to.

    A07b's ``MarkdownChunker`` does not size-check a fenced code block and its ``CodeChunker``
    cannot cut inside a physical line, so both can emit a chunk far above the limit (MEASURED on the
    real vault: 28 446 and 12 623 characters). Until those are fixed, this backstop is what keeps a
    whole embedding batch from being lost.
    """
    from aimemory.chunking.markdown import chunk_markdown_text

    fenced = "# Title\n\n```text\n" + ("payload words " * 4000) + "\n```\n"
    drafts = chunk_markdown_text(fenced, max_tokens=200, overlap_tokens=20)
    assert max(len(d.text) for d in drafts) > EMBED_MAX_CHARS  # the defect being covered for

    bounded, oversized = _bounded_drafts(drafts)
    assert oversized >= 1
    assert all(len(d.text) <= EMBED_MAX_CHARS for d in bounded)
    # Ordinals stay dense and in reading order, and no content is dropped on the floor
    # (``DomainModel`` strips each piece's outer whitespace, so compare the non-whitespace text).
    assert [d.ordinal for d in bounded] == list(range(len(bounded)))
    assert "".join("".join(d.text.split()) for d in bounded) == "".join(
        "".join(d.text.split()) for d in drafts
    )


def test_bounded_drafts_leaves_conforming_drafts_untouched() -> None:
    from aimemory.chunking.markdown import chunk_markdown_text

    drafts = chunk_markdown_text("# A\n\nshort body\n\n## B\n\nanother\n", max_tokens=200)
    bounded, oversized = _bounded_drafts(drafts)
    assert oversized == 0
    assert bounded is drafts or [d.text for d in bounded] == [d.text for d in drafts]


# ==================================================================================================
# Tier 1: the backfill (needs Postgres)
# ==================================================================================================


def _ingest(root: Path, session, tier: int, embedder) -> object:
    from aimemory.cli.ingest import run_ingestion

    return run_ingestion(
        root,
        session=session,
        project_id="fixture-project",
        label="mini-vault-fixture",
        root_id="mini-vault-fixture",
        scheme="localfs",
        device_id="local-development-machine",
        tier=tier,
        embedder=embedder,
    )


#: The scenario suite shares the dev database with real ingested data (``pg_session`` rolls the
#: test back, it does not give it an empty database), so every count here is scoped to the throwaway
#: root this module ingests. A bare ``SELECT count(*) FROM chunks`` would measure the vault.
FIXTURE_ROOT_ID = "mini-vault-fixture"


def _fixture_chunks(session) -> int:
    return int(
        session.execute(
            sa.text(
                "SELECT count(*) FROM chunks c JOIN sources s ON s.id = c.source_id "
                "WHERE s.root_id = :rid"
            ),
            {"rid": FIXTURE_ROOT_ID},
        ).scalar()
        or 0
    )


def _fixture_chunks_without_embedding(session) -> int:
    return int(
        session.execute(
            sa.text(
                "SELECT count(*) FROM chunks c JOIN sources s ON s.id = c.source_id "
                "WHERE s.root_id = :rid AND NOT EXISTS "
                "(SELECT 1 FROM embeddings e WHERE e.text_hash = c.text_hash)"
            ),
            {"rid": FIXTURE_ROOT_ID},
        ).scalar()
        or 0
    )


def test_tier1_scan_backfills_content_a_tier0_scan_catalogued(tmp_source_root, pg_session) -> None:
    """``run --tier 0`` then ``run --tier 1`` must embed the vault.

    Before this was fixed the second scan classified all 10 fixture files as ``unchanged`` and
    returned without writing a single chunk, because only ``_process`` (reached by changed versions)
    ever ran the content stages. The operator-visible symptom was an empty ``chunks`` table after a
    apparently successful pair of runs.
    """
    from test_change_detection import _FakeEmbedder  # noqa: PLC0415

    embedder = _FakeEmbedder()

    tier0 = _ingest(tmp_source_root.root, pg_session, 0, embedder)
    assert tier0.counters.get("new", 0) > 0
    assert _fixture_chunks(pg_session) == 0
    assert embedder.calls == 0

    tier1 = _ingest(tmp_source_root.root, pg_session, 1, embedder)
    assert tier1.counters.get("unchanged", 0) > 0  # nothing changed on disk...
    assert tier1.counters.get("backfilled_versions", 0) > 0  # ...but the work was still owed
    assert tier1.counters.get("chunks_written", 0) > 0
    assert tier1.counters.get("embeddings_written", 0) > 0
    assert _fixture_chunks(pg_session) > 0
    assert _fixture_chunks_without_embedding(pg_session) == 0


def test_third_scan_does_nothing_and_writes_nothing(tmp_source_root, pg_session) -> None:
    """A settled corpus is a no-op: the backfill gate must not re-run finished stages."""
    from test_change_detection import _FakeEmbedder  # noqa: PLC0415

    embedder = _FakeEmbedder()
    _ingest(tmp_source_root.root, pg_session, 1, embedder)
    chunks_after_first = _fixture_chunks(pg_session)
    calls_after_first = embedder.calls

    report = _ingest(tmp_source_root.root, pg_session, 1, embedder)

    assert report.counters.get("backfilled_versions", 0) == 0
    assert report.counters.get("chunks_written", 0) == 0
    assert embedder.calls == calls_after_first
    assert _fixture_chunks(pg_session) == chunks_after_first


def test_file_with_no_extractor_is_downgraded_to_catalog_only_with_a_reason(
    tmp_source_root, pg_session
) -> None:
    """The downgrade has to survive the ``_StageSkipped`` that triggers it.

    It was written on the stage's own session, which :meth:`IngestionPipeline._stage` rolls back
    when the skip propagates - so the source stayed INDEX_CONTENT with no text and no recorded
    reason, and counted against Tier 1 coverage for ever. MEASURED on the real vault: six files.
    """
    from aimemory.persistence.repositories import SourceRepo
    from test_change_detection import _FakeEmbedder, _uri  # noqa: PLC0415

    rel = "00 Inbox/no-extension-artifact"
    (tmp_source_root.root / rel).write_text("a LaTeX build log or similar\n", encoding="utf-8")

    _ingest(tmp_source_root.root, pg_session, 1, _FakeEmbedder())

    source = SourceRepo(pg_session).get_by_uri(_uri(rel))
    assert source is not None
    assert source.policy.value == "CATALOG_ONLY"
    assert "no extractor" in (source.policy_reason or "")
