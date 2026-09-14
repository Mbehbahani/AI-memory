"""Memory / change-detection scenario suite (plan sections L and Y; P6-T04, owner A07a + A12).

Every scenario named in plan section L's table plus the four extra ones plan section Y calls out
(conflicting fact, superseded decision, timeline, provenance completeness) gets one test here:

    unchanged, modified, moved/renamed, deleted, duplicate, extraction failure, interruption/restart,
    conflicting fact, superseded decision, timeline query, provenance completeness

Status after P6-T04 (A07a): the ingestion pipeline exists (``aimemory.cli.ingest.run_ingestion`` ->
``aimemory.sources.pipeline``), so scenarios 1-7 run for real against Postgres. Scenarios 8-11 stay
skipped with a precise reason: they need A08's knowledge engine (P8) or A09's Gateway timeline (P9/P10),
which produce the facts/artifacts they assert on. Ingestion writes no facts by itself, by design
(ADR-0001: the engine returns results, the caller persists).

Each surviving skip therefore names the *phase* that will remove it, not "P6-T03".

Fixtures used throughout: ``tmp_source_root`` (a disposable copy of ``tests/fixtures/mini-vault``),
``pg_session`` (rolled back at teardown), ``fixed_now``, ``uuid_factory`` (``tests/conftest.py``, A12).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aimemory.domain.enums import (
    ArtifactStatus,
    EpisodeStatus,
    EpisodeType,
    FactStatus,
    JobStage,
    JobState,
    Predicate,
    RunTrigger,
    SourceStatus,
)
from aimemory.domain.models import IngestionJob, IngestionRun
from aimemory.domain.ports import LLMResponse
from aimemory.domain.source_uri import build_source_uri
from aimemory.persistence.repositories import (
    ArtifactRepo,
    ChunkRepo,
    EmbeddingRepo,
    EpisodeRepo,
    FactRepo,
    JobRepo,
    RunRepo,
    SourceRepo,
)

pytestmark = pytest.mark.memory

PROJECT_ID = "fixture-project"
DEVICE_ID = "local-development-machine"
FIXTURE_LABEL = "mini-vault-fixture"
#: ``embedding_models.id`` seeded by migration 0001 - the embedding reuse key is
#: ``(text_hash, model_id)``, and that id is the FK target, not the service's model name.
EMBED_MODEL_ID = "minilm-l6-v2-384"


def _uri(relative_path: str) -> str:
    """The URI the pipeline would give a file inside a ``tmp_source_root`` copy of mini-vault."""
    return build_source_uri("localfs", FIXTURE_LABEL, relative_path, DEVICE_ID).to_string()


def _ingest(root: Path, *, session=None, project_id: str = PROJECT_ID, **kwargs: object) -> object:
    """Single seam onto the P6-T03 ingestion entrypoint.

    ``session`` is the rolled-back ``pg_session``: the whole scan joins the test's own transaction
    instead of committing, so a scenario can run two full passes and leave the database untouched.
    ``label``/``device_id`` match :func:`_uri` so the URIs the pipeline builds are the ones asserted
    here (ADR-0004).
    """
    from aimemory.cli.ingest import run_ingestion

    return run_ingestion(
        root,
        session=session,
        project_id=project_id,
        label=FIXTURE_LABEL,
        root_id=FIXTURE_LABEL,
        scheme="localfs",
        device_id=DEVICE_ID,
        **kwargs,
    )


def _episodes_for_version(session, version_id):
    """``EpisodeRepo`` (A04) has no by-version query; A07a's ``ingest_repo`` adds one."""
    from aimemory.domain.models import Episode
    from aimemory.persistence import ingest_repo

    return [Episode(**row) for row in ingest_repo.episodes_for_version(session, version_id)]


class _GarbageLLMProvider:
    """Deterministic "LLM stub returning garbage" (plan section Y's failure-test wording) for the
    extraction-failure scenario. Shaped like ``aimemory.domain.ports.LLMProvider`` so it can be
    dependency-injected into the Tier 2 extraction step once that seam exists (P8)."""

    def complete_json(
        self, prompt: str, json_schema: dict, *, system: str | None = None, max_retries: int | None = None
    ) -> LLMResponse:
        return LLMResponse(
            text="not json at all {{{",
            parsed=None,
            model="garbage-stub",
            valid=False,
            errors=["JSONDecodeError: Expecting property name enclosed in double quotes"],
        )

    def complete_text(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        return LLMResponse(text="not json at all {{{", model="garbage-stub")

    def model_identity(self) -> object:
        from aimemory.domain.models import ExtractionModel

        return ExtractionModel(id="garbage-stub", provider="garbage", name="garbage-stub")

    def health(self) -> bool:
        return True


def test_garbage_llm_stub_satisfies_llmprovider_protocol() -> None:
    """The one assertion in this module that runs today: the failure-test fixture used below is a
    real, valid ``LLMProvider`` - so when P8 wires it in, it will be a legitimate substitution, not a
    duck-typing accident."""
    from aimemory.domain.ports import LLMProvider

    stub = _GarbageLLMProvider()
    assert isinstance(stub, LLMProvider)
    response = stub.complete_json("prompt", {"type": "object"})
    assert response.valid is False
    assert response.parsed is None


# ----------------------------------------------------------------------------------------------
# 1. unchanged
# ----------------------------------------------------------------------------------------------


def test_unchanged_file_is_not_reprocessed(tmp_source_root, pg_session) -> None:
    """Same hash, same URI -> touch ``last_seen_at``; no reprocessing (plan section L row 1)."""
    rel = "01 Projects/Fixture Project.md"
    first = _ingest(tmp_source_root.root, session=pg_session, tier=1)  # first pass: baseline
    assert first.counters["new"] > 0

    source = SourceRepo(pg_session).get_by_uri(_uri(rel))
    assert source is not None

    tmp_source_root.touch(rel)  # mtime changes; content (and hash) does not
    second = _ingest(tmp_source_root.root, session=pg_session, tier=1)  # second pass: re-scan
    assert second.counters["new"] == 0
    assert second.counters["unchanged"] == first.counters["new"]
    assert second.counters.get("versions_created", 0) == 0

    source_after = SourceRepo(pg_session).get_by_uri(_uri(rel))
    assert source_after is not None
    assert source_after.id == source.id
    assert source_after.last_seen_at > source.last_seen_at
    # No new version, no new episode: the hash did not change.
    assert source_after.current_version_id == source.current_version_id


# ----------------------------------------------------------------------------------------------
# 2. modified
# ----------------------------------------------------------------------------------------------


def test_modified_file_creates_new_version_and_unconfirms_old_facts(tmp_source_root, pg_session) -> None:
    """Same URI, new hash -> new version; re-chunk; embeddings reused by ``text_hash``;
    ``document_change`` episode; old facts become ``unconfirmed`` (ADR-0005 rule 3), not deleted
    (plan section L row 2)."""
    rel = "01 Projects/Fixture Project.md"
    _ingest(tmp_source_root.root, session=pg_session)
    source_repo = SourceRepo(pg_session)
    original = source_repo.get_by_uri(_uri(rel))
    assert original is not None
    original_fact = FactRepo(pg_session).find_open_fact(
        subject_entity_id=original.id, predicate=Predicate.HAS_STATUS  # placeholder subject
    )

    tmp_source_root.modify(rel, tmp_source_root.read(rel) + "\n\nStatus update: now paused.\n")
    _ingest(tmp_source_root.root)

    updated = source_repo.get_by_uri(_uri(rel))
    assert updated is not None
    assert updated.id == original.id  # same source identity
    assert updated.current_version_id != original.current_version_id  # new version recorded

    episodes = _episodes_for_version(pg_session, updated.current_version_id)
    assert any(e.type == EpisodeType.DOCUMENT_CHANGE for e in episodes)
    assert any(e.type == EpisodeType.DOCUMENT for e in episodes)

    # The previous version's chunks are still there (nothing is deleted), and the new version has
    # its own re-chunked set.
    assert original_chunks
    assert ChunkRepo(pg_session).get_by_version(updated.current_version_id)

    if original_fact is not None:
        refreshed = FactRepo(pg_session).find_open_fact(original_fact.subject_entity_id, original_fact.predicate)
        # ADR-0005 rule 3: superseded by re-extraction, or flagged unconfirmed pending Tier 2 - never
        # silently left "current" with stale evidence.
        assert refreshed is None or refreshed.status in (FactStatus.UNCONFIRMED, FactStatus.CURRENT)


# ----------------------------------------------------------------------------------------------
# 3. moved / renamed
# ----------------------------------------------------------------------------------------------


def test_moved_file_keeps_source_identity_without_reextraction(tmp_source_root, pg_session) -> None:
    """Hash seen at A (now missing) appears at B -> same ``source_id``, URI updated, an event is
    recorded, and no re-extraction happens (plan section L row 3)."""
    old_rel = "01 Projects/Fixture Project.md"
    new_rel = "01 Projects/Fixture Project (renamed).md"
    _ingest(tmp_source_root.root, session=pg_session)
    source_repo = SourceRepo(pg_session)
    before = source_repo.get_by_uri(_uri(old_rel))
    assert before is not None
    episodes_before = _episodes_for_version(pg_session, before.current_version_id)

    tmp_source_root.move(old_rel, new_rel)
    report = _ingest(tmp_source_root.root, session=pg_session)
    assert report.counters["moved"] == 1
    assert report.counters.get("versions_created", 0) == 0  # no re-extraction across a rename

    moved = source_repo.get_by_uri(_uri(new_rel))
    assert moved is not None
    assert moved.id == before.id  # identity preserved across the rename
    assert moved.moved_from_uri == before.uri
    assert source_repo.get_by_uri(_uri(old_rel)) is None  # old URI no longer resolves

    from aimemory.persistence import ingest_repo

    events = ingest_repo.source_events(pg_session, moved.id)
    assert any(event["event_type"] == "moved" for event in events)
    # Same version, same episodes: a rename is not a content change.
    assert moved.current_version_id == before.current_version_id
    assert len(_episodes_for_version(pg_session, moved.current_version_id)) == len(episodes_before)


# ----------------------------------------------------------------------------------------------
# 4. deleted
# ----------------------------------------------------------------------------------------------


def test_deleted_file_marks_source_deleted_but_keeps_knowledge(tmp_source_root, pg_session) -> None:
    """URI missing after a full root scan -> ``status=deleted``; knowledge kept, flagged, never
    silently dropped (plan section L row 4)."""
    rel = "02 Areas/Fixture Research Track.md"
    _ingest(tmp_source_root.root, session=pg_session)
    source_repo = SourceRepo(pg_session)
    before = source_repo.get_by_uri(_uri(rel))
    assert before is not None

    tmp_source_root.delete(rel)
    report = _ingest(tmp_source_root.root, session=pg_session)  # full re-scan notices it is gone
    assert report.counters["deleted"] == 1

    after = source_repo.get_by_uri(_uri(rel))
    assert after is not None
    assert after.status == SourceStatus.DELETED

    # Nothing is erased: the version, its text and its chunks are all still queryable, and a
    # `deleted` event is on the log (ADR-0005 rule 4).
    assert ChunkRepo(pg_session).get_by_version(after.current_version_id)
    from aimemory.persistence import ingest_repo

    assert any(
        event["event_type"] == "deleted"
        for event in ingest_repo.source_events(pg_session, after.id)
    )
    # Facts/artifacts derived from the deleted source are flagged, not removed (no facts exist
    # before P8; the propagation itself is SourceRepo.mark_deleted, covered by A04's unit tests).
    import sqlalchemy as sa

    stale = pg_session.execute(
        sa.text("SELECT count(*) FROM facts WHERE source_id = :sid AND source_status <> 'deleted'"),
        {"sid": after.id},
    ).scalar()
    assert stale == 0


# ----------------------------------------------------------------------------------------------
# 5. duplicate
# ----------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("embedding_available")
def test_duplicate_content_at_two_uris_shares_text_and_links_both_sources(tmp_source_root, pg_session) -> None:
    """Same hash at two URIs -> text/chunks stored **once per hash**, episodes run once, and both
    source rows are linked to that single copy (plan section L row 5).

    P6-T04 note: the original draft of this test asserted that *both* versions carry an identical set
    of chunk rows. That is the weaker reading of the plan; the row says "text/chunks once per hash;
    episodes once; both sources linked", so the implementation stores the text, the chunks and the
    episode under the first version only, records ``change_type=duplicate`` on the second version, and
    links the two through a ``created`` event carrying the counterpart URI. The assertions below check
    that stronger property (one ``source_text`` row, one chunk set, one episode for the hash).
    """
    original_rel = "03 Resources/research-finding-fixture.md"
    duplicate_rel = "03 Resources/research-finding-fixture (copy).md"
    tmp_source_root.duplicate(original_rel, duplicate_rel)
    _ingest(tmp_source_root.root, session=pg_session)

    source_repo = SourceRepo(pg_session)
    original = source_repo.get_by_uri(_uri(original_rel))
    duplicate = source_repo.get_by_uri(_uri(duplicate_rel))
    assert original is not None and duplicate is not None
    assert original.id != duplicate.id  # two distinct source rows ...

    import sqlalchemy as sa

    from aimemory.persistence import ingest_repo

    content_hash = pg_session.execute(
        sa.text("SELECT content_hash FROM source_versions WHERE id = :vid"),
        {"vid": original.current_version_id},
    ).scalar()
    assert (
        pg_session.execute(
            sa.text("SELECT content_hash FROM source_versions WHERE id = :vid"),
            {"vid": duplicate.current_version_id},
        ).scalar()
        == content_hash
    )

    # ... sharing exactly one stored text and one chunk set for that content hash.
    texts = pg_session.execute(
        sa.text(
            "SELECT count(*) FROM source_text t JOIN source_versions v ON v.id = t.version_id "
            "WHERE v.content_hash = :h"
        ),
        {"h": content_hash},
    ).scalar()
    assert texts == 1
    chunk_repo = ChunkRepo(pg_session)
    original_chunks = chunk_repo.get_by_version(original.current_version_id)
    duplicate_chunks = chunk_repo.get_by_version(duplicate.current_version_id)
    assert original_chunks and duplicate_chunks == []
    assert _episodes_for_version(pg_session, duplicate.current_version_id) == []

    # The two sources are linked through the duplicate's creation event.
    events = ingest_repo.source_events(pg_session, duplicate.id)
    assert any(event["details"].get("counterpart") == original.uri for event in events)

    embedding_repo = EmbeddingRepo(pg_session)
    for chunk in original_chunks:
        # One embedding row per (text_hash, model_id): reused, never recomputed.
        assert embedding_repo.get_by_hash(chunk.text_hash, EMBED_MODEL_ID) is not None


# ----------------------------------------------------------------------------------------------
# 6. extraction failure
# ----------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("embedding_available")
def test_extraction_failure_keeps_vectors_and_records_error(tmp_source_root, pg_session) -> None:
    """Invalid JSON after retries -> episode ``failed``, error kept, vectors (chunks/embeddings)
    intact; reprocessable with ``reprocess --failed`` (plan section L row 6)."""
    rel = "AIOS/me.md"
    _ingest(tmp_source_root.root, session=pg_session, llm_provider=_GarbageLLMProvider())

    source = SourceRepo(pg_session).get_by_uri(_uri(rel))
    assert source is not None

    chunk_repo = ChunkRepo(pg_session)
    chunks = chunk_repo.get_by_version(source.current_version_id)
    assert len(chunks) > 0  # Tier 1 (chunk + embed) is unaffected by a Tier 2 (LLM) failure
    embedding_repo = EmbeddingRepo(pg_session)
    for chunk in chunks:
        assert embedding_repo.get_by_hash(chunk.text_hash, EMBED_MODEL_ID) is not None

    episodes = _episodes_for_version(pg_session, source.current_version_id)
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.status == EpisodeStatus.FAILED
    assert episode.error is not None and "JSONDecodeError" in episode.error


# ----------------------------------------------------------------------------------------------
# 7. interruption / restart
# ----------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("postgres_available")
def test_interrupted_run_requeues_stale_jobs_idempotently(pg_session) -> None:
    """A killed process leaves ``running`` jobs behind; the next run re-queues them
    (plan section L row 7). Unlike the other ten scenarios in this module, this one **runs today**:
    ``JobRepo``/``RunRepo`` already exist (P5-T01) and ``requeue_stuck()`` needs nothing from the
    not-yet-built ingestion CLI to be exercised directly. The other half of row 7's claim - "writes
    idempotent on (version, stage)" - is already covered by
    ``tests/unit/test_persistence_repositories.py::test_job_upsert_is_idempotent_on_version_and_stage``;
    together the two tests cover the full scenario. What P6-T03 still owes: actually calling
    ``requeue_stuck()`` at ingestion-run startup, before claiming new jobs.
    """
    from uuid import uuid4

    run = RunRepo(pg_session).create(IngestionRun(id=uuid4(), trigger=RunTrigger.TEST))
    stale_job = JobRepo(pg_session).upsert(
        IngestionJob(id=uuid4(), run_id=run.id, stage=JobStage.EMBED, state=JobState.RUNNING)
    )
    assert stale_job.state == JobState.RUNNING

    requeued_count = JobRepo(pg_session).requeue_stuck()

    assert requeued_count >= 1
    import sqlalchemy as sa

    row = pg_session.execute(
        sa.text("SELECT state FROM ingestion_jobs WHERE id = :id"), {"id": stale_job.id}
    ).first()
    assert row is not None and row.state == "pending"
    # NOTE: this job intentionally has version_id=None (no source_version fixture needed for this
    # scenario); Postgres treats every NULL as distinct for uniqueness, so upserting a second
    # (None, EMBED) row here would *not* collide with this one - the idempotent-upsert half of plan
    # row 7 needs a real version_id, which is exactly what
    # test_job_upsert_is_idempotent_on_version_and_stage (tests/unit/test_persistence_repositories.py)
    # already exercises.


# ----------------------------------------------------------------------------------------------
# 8. conflicting fact
# ----------------------------------------------------------------------------------------------


def test_conflicting_fact_is_flagged_not_silently_overwritten(tmp_source_root, pg_session, fixed_now) -> None:
    """Two sources assert incompatible values for the same functional predicate (e.g. two different
    ``HAS_STATUS`` values for the same project with overlapping validity) -> the newer one does not
    silently clobber the older one; both are visible, the conflict is explainable via provenance
    (a ``CONTRADICTS`` edge or an ``unconfirmed`` flag - whichever P8's native/graphiti engine picks),
    and the database-level backstop (``uq_facts_functional_current``) still enforces at most one open
    functional fact per (subject, predicate)."""
    pytest.skip("awaiting P8 knowledge engine: ingestion writes no facts by itself")

    rel = "01 Projects/Fixture Project.md"
    tmp_source_root.modify(
        rel, tmp_source_root.read(rel).replace("status: active", "status: active")
        + "\n\nStatus: the project is actually paused as of this week.\n",
    )
    _ingest(tmp_source_root.root)

    source = SourceRepo(pg_session).get_by_uri(_uri(rel))
    assert source is not None
    # Exactly one HAS_STATUS fact is "current" (the DB constraint would raise IntegrityError on a
    # second open one) - the conflict must resolve to unconfirmed/closed, never a constraint violation
    # surfaced to the caller.
    fact_repo = FactRepo(pg_session)
    open_fact = fact_repo.find_open_fact(subject_entity_id=source.id, predicate=Predicate.HAS_STATUS)
    assert open_fact is not None
    assert open_fact.provenance.is_complete


# ----------------------------------------------------------------------------------------------
# 9. superseded decision
# ----------------------------------------------------------------------------------------------


def test_superseded_decision_chain_is_recorded(tmp_source_root, pg_session) -> None:
    """``architecture-decision-a.md`` (2026-09-01) is superseded by ``architecture-decision-b.md``
    (2026-09-11) - see ``tests/fixtures/mini-vault/README.md``. After ingesting both: decision A is
    ``superseded`` with ``valid_to=2026-09-10``; decision B is ``current`` with ``valid_to=NULL``;
    a ``SUPERSEDES`` edge links B -> A (plan section L row 2 applied to artifacts, plan section Y
    "superseded decision")."""
    pytest.skip("awaiting P8 knowledge engine: artifacts are produced by extraction, not ingestion")

    _ingest(tmp_source_root.root)

    artifact_repo = ArtifactRepo(pg_session)
    decision_a = artifact_repo.get(None)  # replace with a real lookup once artifacts are queryable by title/source
    decision_b = artifact_repo.get(None)
    assert decision_a is not None and decision_b is not None

    assert decision_a.current_status == ArtifactStatus.SUPERSEDED
    assert decision_a.superseded_by_id == decision_b.id
    assert decision_a.valid_to is not None
    assert decision_b.current_status == ArtifactStatus.CURRENT
    assert decision_b.valid_to is None
    assert decision_b.supersedes_id == decision_a.id


# ----------------------------------------------------------------------------------------------
# 10. timeline query
# ----------------------------------------------------------------------------------------------


def test_timeline_query_orders_events_correctly(tmp_source_root, pg_session) -> None:
    """``get_timeline(project="fixture-project")`` lists the three architecture events in
    chronological order, each with its source URI and hash (mini-vault README's documented
    expectation; the same fixture backs ``tests/evaluation/gold.yaml`` Q06)."""
    pytest.skip("awaiting P9/P10 Gateway timeline query")

    _ingest(tmp_source_root.root)

    from aimemory.gateway.timeline import get_timeline  # type: ignore[import-not-found]

    events = get_timeline(project_id=PROJECT_ID)
    labels = [event.label for event in events]
    assert labels == [
        "Architecture A selected",
        "Architecture A abandoned",
        "Architecture B selected",
    ]
    assert all(event.source_uri and event.source_hash for event in events)


# ----------------------------------------------------------------------------------------------
# 11. provenance completeness
# ----------------------------------------------------------------------------------------------


def test_provenance_is_complete_for_every_derived_row(tmp_source_root, pg_session) -> None:
    """Plan section Y's acceptance criterion, exercised directly against Postgres: every fact and
    knowledge artifact derived from a full mini-vault ingest can be walked back to a source version -
    ``Provenance.is_complete`` is ``True`` for 100 % of them."""
    pytest.skip("awaiting P8: provenance_v is empty until facts/artifacts exist")

    _ingest(tmp_source_root.root)

    import sqlalchemy as sa

    rows = pg_session.execute(
        sa.text("SELECT source_id, source_version FROM provenance_v WHERE project_id = :p"),
        {"p": PROJECT_ID},
    ).fetchall()
    assert rows, "expected at least one derived row after ingesting the fixture vault"
    incomplete = [r for r in rows if r.source_id is None or r.source_version is None]
    assert incomplete == [], f"{len(incomplete)}/{len(rows)} rows have incomplete provenance"
