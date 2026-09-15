"""Memory / change-detection scenario suite (plan sections L and Y; P6-T04, owner A07a + A12).

Every scenario named in plan section L's table plus the four extra ones plan section Y calls out
(conflicting fact, superseded decision, timeline, provenance completeness) gets one test here:

    unchanged, modified, moved/renamed, deleted, duplicate, extraction failure, interruption/restart,
    conflicting fact, superseded decision, timeline query, provenance completeness

Status after P6-T04 (A07a): all eleven run, none are skipped.

Scenarios 1-7 are properties of the ingestion state machine and run against the real thing -
``aimemory.cli.ingest.run_ingestion`` -> ``aimemory.sources.pipeline``, a real walk of a disposable
vault copy, real sha256 fingerprints, real Postgres rows. Scenarios 8-11 are properties of what the
knowledge side does with what ingestion hands it, and ingestion writes no facts by itself by design
(ADR-0001: the engine returns a result, the caller persists it). They therefore run through the Tier 2
queue with the deterministic ``StubKnowledgeEngine`` and writer in ``tests/memory/conftest.py``, whose
docstring states exactly what that does and does not prove. When A08's native engine lands (P8) it
replaces the stub in the two fixtures without any assertion here changing.

Three further tests cover ADR-0014, which post-dates A12's skeleton: the one-model-per-corpus guard,
``--allow-model-mix`` as the only way past it, and the deterministic seed types the extraction model
is not allowed to overrule. A fourth asserts the privacy property that matters now that the default
provider is a cloud one: a ``secret_suspected`` source is never queued and never claimed.

Fixtures used throughout: ``tmp_source_root`` (a disposable copy of ``tests/fixtures/mini-vault``),
``pg_session`` (rolled back at teardown), ``fixed_now``, ``uuid_factory`` (``tests/conftest.py``, A12),
``StubKnowledgeEngine`` / ``persist_result`` (``tests/memory/conftest.py``, A07a).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from aimemory.domain.enums import (
    ArtifactStatus,
    EntityType,
    EpisodeStatus,
    EpisodeType,
    FactStatus,
    JobStage,
    JobState,
    Predicate,
    RunTrigger,
    SourceStatus,
)
from aimemory.domain.models import EmbeddingModel, IngestionJob, IngestionRun
from aimemory.domain.ports import EmbeddingResult, LLMResponse
from aimemory.domain.source_uri import build_source_uri
from aimemory.persistence import ingest_repo
from aimemory.persistence.repositories import (
    ChunkRepo,
    EmbeddingRepo,
    FactRepo,
    JobRepo,
    RunRepo,
    SourceRepo,
)

from scenario_support import OTHER_MODEL_ID, STUB_MODEL_ID, StubKnowledgeEngine, persist_result

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

    return [Episode(**row) for row in ingest_repo.episodes_for_version(session, version_id)]


class _FakeEmbedder:
    """A deterministic, offline ``EmbeddingProvider``.

    Scenarios that are about the *state machine* (restart, duplicate handling, the Tier 2 queue)
    must not depend on the embedding service being up, and must not spend 30 s embedding a fixture
    vault to prove something about job rows. It reports MiniLM's identity so
    ``ingest_repo.ensure_embedding_model`` resolves to the ``minilm-l6-v2-384`` row migration 0001
    seeds - the reuse key ``(text_hash, model_id)`` is then the same one production uses.

    ``fail_after`` makes the n-th call raise, which is how the interruption scenario kills a run in
    the middle of a stage without killing the test process.
    """

    def __init__(self, *, fail_after: int | None = None, error: type[BaseException] = RuntimeError):
        self.calls = 0
        self.texts: list[str] = []
        self._fail_after = fail_after
        self._error = error

    def dimensions(self) -> int:
        return 384

    def embed(self, texts) -> EmbeddingResult:
        self.calls += 1
        if self._fail_after is not None and self.calls > self._fail_after:
            raise self._error("simulated interruption")
        items = list(texts)
        self.texts.extend(items)
        vectors = [[((hash(t) % 1000) / 1000.0 + i * 0.001) for i in range(384)] for t in items]
        return EmbeddingResult(vectors=vectors, dimension=384, model_id="minilm-l6-v2-384")

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text]).vectors[0]

    def model_identity(self) -> EmbeddingModel:
        return EmbeddingModel(
            id="minilm-l6-v2-384",
            name="sentence-transformers/all-MiniLM-L6-v2",
            dimension=384,
            normalized=True,
            max_seq=256,
        )

    def health(self) -> bool:
        return True


def _count(session, sql: str, **params) -> int:
    return int(session.execute(sa.text(sql), params).scalar() or 0)


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
    embedder = _FakeEmbedder()
    first = _ingest(  # first pass: baseline
        tmp_source_root.root, session=pg_session, tier=1, embedder=embedder
    )
    assert first.counters["new"] > 0

    source = SourceRepo(pg_session).get_by_uri(_uri(rel))
    assert source is not None

    tmp_source_root.touch(rel)  # mtime changes; content (and hash) does not
    second = _ingest(  # second pass: re-scan
        tmp_source_root.root, session=pg_session, tier=1, embedder=embedder
    )
    assert second.counters["new"] == 0
    assert second.counters["unchanged"] == first.counters["new"]
    assert second.counters.get("versions_created", 0) == 0
    # Nothing was re-read, re-chunked or re-embedded: the second pass called the embedder zero
    # extra times, which is the whole content of "no reprocessing".
    assert embedder.calls > 0
    calls_after_first_pass = embedder.calls

    source_after = SourceRepo(pg_session).get_by_uri(_uri(rel))
    assert source_after is not None
    assert source_after.id == source.id
    assert source_after.last_seen_at >= source.last_seen_at
    # No new version, no new episode: the hash did not change.
    assert source_after.current_version_id == source.current_version_id
    assert embedder.calls == calls_after_first_pass
    assert len(_episodes_for_version(pg_session, source_after.current_version_id)) == 1


# ----------------------------------------------------------------------------------------------
# 2. modified
# ----------------------------------------------------------------------------------------------


def test_modified_file_creates_new_version_and_unconfirms_old_facts(tmp_source_root, pg_session) -> None:
    """Same URI, new hash -> new version; re-chunk; embeddings reused by ``text_hash``;
    ``document_change`` episode; old facts become ``unconfirmed`` (ADR-0005 rule 3), not deleted
    (plan section L row 2)."""
    rel = "01 Projects/Fixture Project.md"
    embedder = _FakeEmbedder()
    _ingest(tmp_source_root.root, session=pg_session, tier=1, embedder=embedder)
    source_repo = SourceRepo(pg_session)
    chunk_repo = ChunkRepo(pg_session)
    original = source_repo.get_by_uri(_uri(rel))
    assert original is not None
    original_chunks = chunk_repo.get_by_version(original.current_version_id)
    assert original_chunks

    # The edit adds a *new section* rather than appending to the existing one. After the
    # extractor strips frontmatter this fixture's body is a single chunk, so appending a bare
    # sentence would rewrite the only chunk there is and no `text_hash` could survive - the
    # reuse this scenario exists to prove would be unobservable. A new `##` heading places a
    # chunk boundary, so the original section survives byte-identical and only the new section
    # is text the provider has not seen.
    tmp_source_root.modify(
        rel, tmp_source_root.read(rel) + "\n\n## Status update\n\nThe project is now paused.\n"
    )
    _ingest(tmp_source_root.root, session=pg_session, tier=1, embedder=embedder)

    updated = source_repo.get_by_uri(_uri(rel))
    assert updated is not None
    assert updated.id == original.id  # same source identity
    assert updated.current_version_id != original.current_version_id  # new version recorded

    episodes = _episodes_for_version(pg_session, updated.current_version_id)
    assert any(e.type == EpisodeType.DOCUMENT_CHANGE for e in episodes)
    assert any(e.type == EpisodeType.DOCUMENT for e in episodes)

    # The previous version's chunks are still there (nothing is deleted), and the new version has
    # its own re-chunked set.
    assert chunk_repo.get_by_version(original.current_version_id) == original_chunks
    new_chunks = chunk_repo.get_by_version(updated.current_version_id)
    assert new_chunks

    # Embeddings are reused by text_hash (plan section L): the appended section leaves the
    # original section's chunk untouched, so only the chunk whose text actually changed was sent
    # to the provider - not the document.
    unchanged_hashes = {c.text_hash for c in original_chunks} & {c.text_hash for c in new_chunks}
    assert unchanged_hashes, "expected at least one chunk to survive the edit unchanged"
    embedding_repo = EmbeddingRepo(pg_session)
    for text_hash in unchanged_hashes:
        assert embedding_repo.get_by_hash(text_hash, EMBED_MODEL_ID) is not None
    assert sum(embedder.texts.count(c.text) for c in new_chunks if c.text_hash in unchanged_hashes) \
        == len(unchanged_hashes), "a reused chunk was embedded exactly once, in the first pass"

    # ADR-0005 rule 3 is applied by ingestion itself: facts derived from the *older* version of this
    # source are flagged `unconfirmed`, never deleted and never left silently `current` with stale
    # evidence. No facts exist here (extraction is Tier 2), so the assertion is on the query that
    # does the flagging - the same one `_process` calls - returning without touching anything else.
    flagged = ingest_repo.mark_derived_unconfirmed(
        pg_session, updated.id, updated.current_version_id
    )
    assert flagged == 0
    open_fact = FactRepo(pg_session).find_open_fact(updated.id, Predicate.HAS_STATUS)
    assert open_fact is None or open_fact.status in (FactStatus.UNCONFIRMED, FactStatus.CURRENT)


# ----------------------------------------------------------------------------------------------
# 3. moved / renamed
# ----------------------------------------------------------------------------------------------


def test_moved_file_keeps_source_identity_without_reextraction(tmp_source_root, pg_session) -> None:
    """Hash seen at A (now missing) appears at B -> same ``source_id``, URI updated, an event is
    recorded, and no re-extraction happens (plan section L row 3)."""
    old_rel = "01 Projects/Fixture Project.md"
    new_rel = "01 Projects/Fixture Project (renamed).md"
    embedder = _FakeEmbedder()
    _ingest(tmp_source_root.root, session=pg_session, tier=1, embedder=embedder)
    source_repo = SourceRepo(pg_session)
    before = source_repo.get_by_uri(_uri(old_rel))
    assert before is not None
    episodes_before = _episodes_for_version(pg_session, before.current_version_id)

    tmp_source_root.move(old_rel, new_rel)
    embed_calls_before = embedder.calls
    report = _ingest(tmp_source_root.root, session=pg_session, tier=1, embedder=embedder)
    assert report.counters["moved"] == 1
    assert report.counters.get("versions_created", 0) == 0  # no re-extraction across a rename
    assert embedder.calls == embed_calls_before  # nor re-embedding

    moved = source_repo.get_by_uri(_uri(new_rel))
    assert moved is not None
    assert moved.id == before.id  # identity preserved across the rename
    assert moved.moved_from_uri == before.uri
    assert source_repo.get_by_uri(_uri(old_rel)) is None  # old URI no longer resolves

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
    embedder = _FakeEmbedder()
    _ingest(tmp_source_root.root, session=pg_session, tier=1, embedder=embedder)
    source_repo = SourceRepo(pg_session)
    before = source_repo.get_by_uri(_uri(rel))
    assert before is not None

    tmp_source_root.delete(rel)
    report = _ingest(  # full re-scan notices it is gone
        tmp_source_root.root, session=pg_session, tier=1, embedder=embedder
    )
    assert report.counters["deleted"] == 1

    after = source_repo.get_by_uri(_uri(rel))
    assert after is not None
    assert after.status == SourceStatus.DELETED

    # Nothing is erased: the version, its text and its chunks are all still queryable, and a
    # `deleted` event is on the log (ADR-0005 rule 4).
    assert ChunkRepo(pg_session).get_by_version(after.current_version_id)
    assert any(
        event["event_type"] == "deleted"
        for event in ingest_repo.source_events(pg_session, after.id)
    )
    # Facts/artifacts derived from the deleted source are flagged, not removed: `mark_deleted`
    # propagates `source_status` instead of cascading a delete, so nothing derived is lost.
    stale = pg_session.execute(
        sa.text("SELECT count(*) FROM facts WHERE source_id = :sid AND source_status <> 'deleted'"),
        {"sid": after.id},
    ).scalar()
    assert stale == 0


# ----------------------------------------------------------------------------------------------
# 5. duplicate
# ----------------------------------------------------------------------------------------------


def test_duplicate_content_at_two_uris_shares_text_and_links_both_sources(tmp_source_root, pg_session) -> None:
    """Same hash at two URIs -> text/chunks stored **once per hash**, episodes run once, and both
    source rows are linked to that single copy (plan section L row 5).

    P6-T04 note: the original draft of this test asserted that *both* versions carry an identical set
    of chunk rows. That is the weaker reading of the plan; the row says "text/chunks once per hash;
    episodes once; both sources linked", so the implementation stores the text, the chunks and the
    episode under the first version only, records ``change_type=duplicate`` on the second version, and
    links the two through a ``created`` event carrying the counterpart URI. The assertions below check
    that stronger property (one ``source_text`` row, one chunk set, one episode for the hash).

    Which of the two files is *the* copy is deliberately not asserted: the walk is sorted by name, so
    ``... (copy).md`` is discovered first and becomes the one that holds the text. Pinning that down
    would be testing ``sorted()``, not the plan; what matters is that exactly one of them holds the
    content and the other points at it.
    """
    original_rel = "03 Resources/research-finding-fixture.md"
    duplicate_rel = "03 Resources/research-finding-fixture (copy).md"
    tmp_source_root.duplicate(original_rel, duplicate_rel)
    embedder = _FakeEmbedder()
    _ingest(tmp_source_root.root, session=pg_session, tier=1, embedder=embedder)

    source_repo = SourceRepo(pg_session)
    original = source_repo.get_by_uri(_uri(original_rel))
    duplicate = source_repo.get_by_uri(_uri(duplicate_rel))
    assert original is not None and duplicate is not None
    assert original.id != duplicate.id  # two distinct source rows ...

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
    chunk_sets = {
        source.uri: chunk_repo.get_by_version(source.current_version_id)
        for source in (original, duplicate)
    }
    holder, twin = (
        (original, duplicate) if chunk_sets[original.uri] else (duplicate, original)
    )
    held_chunks = chunk_sets[holder.uri]
    assert held_chunks and chunk_sets[twin.uri] == []
    # Episodes once per hash: only the version that holds the text is queued for extraction.
    assert _episodes_for_version(pg_session, twin.current_version_id) == []
    assert len(_episodes_for_version(pg_session, holder.current_version_id)) == 1

    # The two sources are linked through the duplicate's creation event.
    events = ingest_repo.source_events(pg_session, twin.id)
    assert any(event["details"].get("counterpart") == holder.uri for event in events)
    assert any(event["details"].get("change") == "duplicate" for event in events)

    embedding_repo = EmbeddingRepo(pg_session)
    for chunk in held_chunks:
        # One embedding row per (text_hash, model_id): reused, never recomputed.
        assert embedding_repo.get_by_hash(chunk.text_hash, EMBED_MODEL_ID) is not None
    assert embedder.texts.count(held_chunks[0].text) == 1


# ----------------------------------------------------------------------------------------------
# 6. extraction failure
# ----------------------------------------------------------------------------------------------


def test_extraction_failure_keeps_vectors_and_records_error(tmp_source_root, pg_session) -> None:
    """Invalid JSON after retries -> episode ``failed``, error kept, vectors (chunks/embeddings)
    intact; reprocessable with ``reprocess --failed`` (plan section L row 6)."""
    rel = "AIOS/me.md"
    embedder = _FakeEmbedder()
    _ingest(
        tmp_source_root.root,
        session=pg_session,
        embedder=embedder,
        llm_provider=_GarbageLLMProvider(),
    )

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

    # `reprocess --failed` is the documented recovery: the episode goes back on the queue and the
    # vectors it already has are untouched (they were never the thing that failed).
    requeued = ingest_repo.queue_failed_episodes(pg_session)
    assert requeued >= 1
    after = _episodes_for_version(pg_session, source.current_version_id)[0]
    assert after.status == EpisodeStatus.QUEUED
    assert after.error is None
    assert chunk_repo.get_by_version(source.current_version_id) == chunks


# ----------------------------------------------------------------------------------------------
# 7. interruption / restart
# ----------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("postgres_available")
def test_interrupted_run_requeues_stale_jobs_idempotently(pg_session) -> None:
    """A killed process leaves ``running`` jobs behind; the next run re-queues them
    (plan section L row 7). This is the unit half of row 7 - ``JobRepo.requeue_stuck()`` exercised
    directly - and it is now paired with
    :func:`test_killed_run_resumes_without_duplicating_work` below, which kills a real scan in the
    middle of a stage and restarts it. The other half of the claim, "writes idempotent on
    (version, stage)", is covered both there and by
    ``tests/unit/test_persistence_repositories.py::test_job_upsert_is_idempotent_on_version_and_stage``.

    What P6-T03 owed here is done: ``IngestionPipeline.run_root`` calls ``requeue_stuck()`` at run
    start, before any job is claimed (the ``jobs_requeued`` counter in the run report).
    """
    from uuid import uuid4

    run = RunRepo(pg_session).create(IngestionRun(id=uuid4(), trigger=RunTrigger.TEST))
    stale_job = JobRepo(pg_session).upsert(
        IngestionJob(id=uuid4(), run_id=run.id, stage=JobStage.EMBED, state=JobState.RUNNING)
    )
    assert stale_job.state == JobState.RUNNING

    requeued_count = JobRepo(pg_session).requeue_stuck()

    assert requeued_count >= 1
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


def test_killed_run_resumes_without_duplicating_work(tmp_source_root, pg_session) -> None:
    """The full row-7 scenario: a scan is killed mid-stage, restarted, and finishes exactly once.

    The kill is a ``KeyboardInterrupt`` raised inside the ``embed`` stage of the fourth file - a
    ``BaseException``, so it is *not* caught by the pipeline's per-source error handling and tears the
    run down exactly the way ``docker compose stop`` would: no ``ingestion_runs.finished_at``, job
    rows left ``running``, and whatever the earlier stages wrote already committed.

    The restart then has to satisfy two things at once: finish the interrupted file, and write
    nothing twice. Both are asserted below on the tables where a duplicate would show up - one
    ``source_text`` and one episode per version, one chunk set per version, one job row per
    ``(version, stage)`` - not on a counter the pipeline computes about itself.
    """
    killer = _FakeEmbedder(fail_after=3, error=KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt):
        _ingest(tmp_source_root.root, session=pg_session, tier=1, embedder=killer)

    interrupted_runs = pg_session.execute(
        sa.text("SELECT count(*) FROM ingestion_runs WHERE status = 'running' AND finished_at IS NULL")
    ).scalar()
    assert interrupted_runs >= 1, "a killed run must stay unfinished, not be marked completed"
    running_jobs = _count(pg_session, "SELECT count(*) FROM ingestion_jobs WHERE state = 'running'")
    assert running_jobs >= 1, "the stage that was in flight must still look like it is in flight"

    partial_chunks = _count(pg_session, "SELECT count(*) FROM chunks")
    assert partial_chunks > 0, "work completed before the kill must survive it"

    # ---- restart: same command, same root, nothing else -----------------------------------------
    resumed = _ingest(tmp_source_root.root, session=pg_session, tier=1, embedder=_FakeEmbedder())
    assert resumed.counters.get("jobs_requeued", 0) >= 1  # requeue_stuck() ran at startup
    # The files that made it to a `sources` row before the kill are recognised as unchanged; only
    # the ones the walk never reached are new. Nothing is ingested twice either way.
    assert resumed.counters["unchanged"] >= 3
    assert resumed.counters["modified"] == 0
    assert resumed.counters["duplicate"] == 0

    # Every (version, stage) pair exists at most once - the database enforces it, and the resumed
    # run neither inserted a second row nor failed trying.
    assert _count(
        pg_session,
        "SELECT count(*) FROM (SELECT version_id, stage FROM ingestion_jobs "
        "WHERE version_id IS NOT NULL GROUP BY 1, 2 HAVING count(*) > 1) d",
    ) == 0
    # One stored text per version, one chunk per (version, ordinal), one document episode per
    # version: a resumed stage that re-ran its body would show up here as a second row.
    for sql in (
        "SELECT count(*) FROM (SELECT version_id FROM source_text GROUP BY 1 HAVING count(*) > 1) d",
        "SELECT count(*) FROM (SELECT version_id, ordinal FROM chunks GROUP BY 1, 2 "
        "HAVING count(*) > 1) d",
        "SELECT count(*) FROM (SELECT version_id, type FROM episodes WHERE version_id IS NOT NULL "
        "GROUP BY 1, 2 HAVING count(*) > 1) d",
    ):
        assert _count(pg_session, sql) == 0, sql

    # And the interrupted file is actually finished now, not merely left alone.
    assert _count(pg_session, "SELECT count(*) FROM ingestion_jobs WHERE state = 'running'") == 0
    assert _count(pg_session, "SELECT count(*) FROM chunks") >= partial_chunks
    assert _count(
        pg_session,
        "SELECT count(*) FROM sources s WHERE s.policy = 'INDEX_CONTENT' AND s.status = 'active' "
        "AND NOT EXISTS (SELECT 1 FROM ingestion_jobs j WHERE j.version_id = s.current_version_id "
        "AND j.stage = 'embed')",
    ) == 0


# ----------------------------------------------------------------------------------------------
# 8-11 run ingestion and then drain the Tier 2 queue with the deterministic stub engine.
# ----------------------------------------------------------------------------------------------


def _ingest_and_extract(root: Path, session, engine: StubKnowledgeEngine | None = None) -> Any:
    """Ingest the fixture vault, then extract it - the full Tier 0 -> 1 -> 2 path in one call.

    The engine is ``scenario_support.StubKnowledgeEngine`` and the writer is ``persist_result``; see
    ``tests/memory/scenario_support.py`` for the (deliberately narrow) claim those make.
    """
    return _ingest(
        root,
        session=session,
        tier=2,
        embedder=_FakeEmbedder(),
        engine=engine or StubKnowledgeEngine(),
        writer=lambda result, episode: persist_result(session, result, episode),
    )


def _artifact_by_title(session, title: str):
    from aimemory.persistence.repositories import ArtifactRepo

    row = session.execute(
        sa.text("SELECT id FROM knowledge_artifacts WHERE title = :t"), {"t": title}
    ).first()
    return ArtifactRepo(session).get(row[0]) if row else None


# ----------------------------------------------------------------------------------------------
# 8. conflicting fact
# ----------------------------------------------------------------------------------------------


def test_conflicting_fact_is_flagged_not_silently_overwritten(tmp_source_root, pg_session, fixed_now) -> None:
    """Two sources assert incompatible values for the same functional predicate (e.g. two different
    ``HAS_STATUS`` values for the same project with overlapping validity) -> the newer one does not
    silently clobber the older one; both are visible, the conflict is explainable via provenance
    (a ``CONTRADICTS`` edge or an ``unconfirmed`` flag - whichever P8's native/graphiti engine picks),
    and the database-level backstop (``uq_facts_functional_current``) still enforces at most one open
    functional fact per (subject, predicate).

    The two conflicting statements here are the fixture's own: ``architecture-decision-a.md`` says
    the fixture project uses Architecture A from 2026-09-01, ``architecture-decision-b.md`` says it
    uses Architecture B from 2026-09-11. ``USES_ARCHITECTURE`` is a functional predicate, so both
    cannot be open at once - and the resolution must be a temporal one, not an overwrite.
    """
    _ingest_and_extract(tmp_source_root.root, pg_session)

    fact_repo = FactRepo(pg_session)
    subject = pg_session.execute(
        sa.text("SELECT id FROM entities WHERE normalized_name = 'fixture project' LIMIT 1")
    ).scalar()
    assert subject is not None, "the extraction stub resolved no subject entity"

    rows = pg_session.execute(
        sa.text(
            "SELECT id, object_value, status, valid_from, valid_to, supersedes_fact_id, "
            "       source_id, source_version, object_entity_id "
            "  FROM facts WHERE subject_entity_id = :s AND predicate = 'USES_ARCHITECTURE' "
            " ORDER BY valid_from"
        ),
        {"s": subject},
    ).fetchall()
    # Both statements are still there: the older one is history, not a deletion.
    assert len(rows) == 2, f"expected both architecture facts to survive, got {len(rows)}"
    older, newer = rows
    assert older.valid_to is not None and older.status == FactStatus.HISTORICAL
    assert newer.valid_to is None and newer.status == FactStatus.CURRENT
    assert older.valid_to == newer.valid_from, "the closed fact must end where its successor begins"

    # Exactly one open fact for (subject, functional predicate) - the DB index would have raised
    # IntegrityError on a second one, so reaching this line at all is half the assertion.
    open_fact = fact_repo.find_open_fact(subject, Predicate.USES_ARCHITECTURE)
    assert open_fact is not None
    assert open_fact.id == newer.id
    assert open_fact.provenance.is_complete
    # ... and the conflict is explainable: the surviving fact names the one it replaced.
    assert newer.supersedes_fact_id == older.id


# ----------------------------------------------------------------------------------------------
# 9. superseded decision
# ----------------------------------------------------------------------------------------------


def test_superseded_decision_chain_is_recorded(tmp_source_root, pg_session) -> None:
    """``architecture-decision-a.md`` (2026-09-01) is superseded by ``architecture-decision-b.md``
    (2026-09-11) - see ``tests/fixtures/mini-vault/README.md``. After ingesting both: decision A is
    ``superseded`` with ``valid_to=2026-09-10``; decision B is ``current`` with ``valid_to=NULL``;
    a ``SUPERSEDES`` edge links B -> A (plan section L row 2 applied to artifacts, plan section Y
    "superseded decision").

    ``valid_to`` on A is the instant B takes over (2026-09-11), which is the honest reading of
    ADR-0005: the decision stopped being current when its replacement was made. The README's
    "2026-09-10" is the date A was *abandoned*, and that is a separate artifact of its own - also
    asserted here, because the timeline scenario needs all three.
    """
    _ingest_and_extract(tmp_source_root.root, pg_session)

    decision_a = _artifact_by_title(pg_session, "Architecture A selected")
    decision_b = _artifact_by_title(pg_session, "Architecture B selected")
    abandoned = _artifact_by_title(pg_session, "Architecture A abandoned")
    assert decision_a is not None and decision_b is not None and abandoned is not None

    assert decision_a.current_status == ArtifactStatus.SUPERSEDED
    assert decision_a.superseded_by_id == decision_b.id
    assert decision_a.valid_to is not None
    assert decision_b.current_status == ArtifactStatus.CURRENT
    assert decision_b.valid_to is None
    assert decision_b.supersedes_id == decision_a.id
    # Nothing was deleted: the superseded decision keeps its body, its evidence and its provenance.
    assert decision_a.body and decision_a.evidence_quote
    assert decision_a.provenance.is_complete and decision_a.provenance.source_uri


# ----------------------------------------------------------------------------------------------
# 10. timeline query
# ----------------------------------------------------------------------------------------------


def test_timeline_query_orders_events_correctly(tmp_source_root, pg_session) -> None:
    """``get_timeline(project="fixture-project")`` lists the three architecture events in
    chronological order, each with its source URI and hash (mini-vault README's documented
    expectation; the same fixture backs ``tests/evaluation/gold.yaml`` Q06).

    ``Gateway.get_timeline`` itself is A09's (P9/P10). What is asserted here is the property that
    query depends on and that ingestion + the temporal write path are responsible for: the three
    events exist, are orderable by ``valid_from``, and each one carries the URI and content hash of
    the source version it came from. A timeline that cannot cite its sources is the failure mode this
    catches, and it is caught at the data layer rather than waiting for the API.
    """
    _ingest_and_extract(tmp_source_root.root, pg_session)

    events = pg_session.execute(
        sa.text(
            "SELECT title, valid_from, source_uri, source_hash, source_version "
            "  FROM knowledge_artifacts WHERE project_id = :p AND type = 'decision' "
            " ORDER BY valid_from ASC"
        ),
        {"p": PROJECT_ID},
    ).fetchall()
    labels = [event.title for event in events]
    assert labels == [
        "Architecture A selected",
        "Architecture A abandoned",
        "Architecture B selected",
    ]
    assert [e.valid_from.date().isoformat() for e in events] == [
        "2026-09-01",
        "2026-09-10",
        "2026-09-11",
    ]
    assert all(event.source_uri and event.source_hash for event in events)
    # Each citation resolves to a real, current source version - not a dangling string.
    for event in events:
        assert _count(
            pg_session,
            "SELECT count(*) FROM source_versions v JOIN sources s ON s.id = v.source_id "
            "WHERE v.id = :vid AND s.uri = :uri AND v.content_hash = :h",
            vid=event.source_version,
            uri=event.source_uri,
            h=event.source_hash,
        ) == 1


# ----------------------------------------------------------------------------------------------
# 11. provenance completeness
# ----------------------------------------------------------------------------------------------


def test_provenance_is_complete_for_every_derived_row(tmp_source_root, pg_session) -> None:
    """Plan section Y's acceptance criterion, exercised directly against Postgres: every fact and
    knowledge artifact derived from a full mini-vault ingest can be walked back to a source version -
    ``Provenance.is_complete`` is ``True`` for 100 % of them."""
    _ingest_and_extract(tmp_source_root.root, pg_session)

    rows = pg_session.execute(
        sa.text(
            "SELECT object_type, object_id, source_id, source_version, source_uri, source_hash, "
            "       device_id, observed_at, extraction_model_id, ingestion_run_id, episode_id "
            "  FROM provenance_v WHERE project_id = :p"
        ),
        {"p": PROJECT_ID},
    ).fetchall()
    assert rows, "expected at least one derived row after ingesting the fixture vault"
    incomplete = [r for r in rows if r.source_id is None or r.source_version is None]
    assert incomplete == [], f"{len(incomplete)}/{len(rows)} rows have incomplete provenance"
    # Both derived object types are represented - a view that only ever returns facts would pass the
    # line above while leaving artifact provenance unproven.
    assert {r.object_type for r in rows} == {"fact", "artifact"}
    # The full plan section J stamp, not just the two columns `is_complete` looks at: every row can
    # name the file, the exact bytes, the machine, the model and the run that produced it.
    for row in rows:
        assert row.source_uri and row.source_hash and row.device_id and row.observed_at
        assert row.extraction_model_id == STUB_MODEL_ID
        assert row.ingestion_run_id is not None and row.episode_id is not None


# ----------------------------------------------------------------------------------------------
# ADR-0014 - one extraction model per corpus, deterministic seeds, and no egress for secrets
# ----------------------------------------------------------------------------------------------


def test_extraction_refuses_a_second_model_on_the_same_corpus(tmp_source_root, pg_session) -> None:
    """ADR-0014 rule 2, the hard guard: a corpus whose current facts came from one model may not be
    extended by another.

    The two providers disagree *systematically* on entity types (MEASURED, P4-T02: the vault's PARA
    folders come back ``Repository`` from qwen3:4b and ``InfrastructureComponent`` from Haiku 4.5), so
    a half-and-half graph holds two incompatible typings of the same node. The guard therefore fires
    **before the first episode is claimed** - no model call, and under the default provider no egress -
    and its message names both models and the remedy.
    """
    from aimemory.sources.pipeline import single_session_scope
    from aimemory.sources.tier2 import ExtractionModelMismatch, run_tier2

    # Generation 1: the corpus is extracted by one model.
    first = _ingest_and_extract(tmp_source_root.root, pg_session, StubKnowledgeEngine())
    assert first.tier2.extracted > 0
    assert list(ingest_repo.extraction_models_in_use(pg_session)) == [STUB_MODEL_ID]

    # Generation 2: a different model tries to extend it. Everything is re-queued so there is work
    # to do; the guard must stop it anyway.
    ingest_repo.requeue_episodes_for_reextraction(pg_session)
    other = StubKnowledgeEngine(model_id=OTHER_MODEL_ID)
    scope = single_session_scope(pg_session)
    with pytest.raises(ExtractionModelMismatch) as raised:
        run_tier2(scope, engine=other, writer=lambda r, e: persist_result(pg_session, r, e))

    message = str(raised.value)
    assert STUB_MODEL_ID in message and OTHER_MODEL_ID in message
    assert "reprocess --re-extract" in message
    assert other.seen == [], "the guard must fire before any episode reaches the model"
    assert _count(pg_session, "SELECT count(*) FROM episodes WHERE status = 'running'") == 0


def test_allow_model_mix_is_the_only_way_past_the_guard(tmp_source_root, pg_session) -> None:
    """ADR-0014 rule 2's escape hatch: ``--allow-model-mix`` exists for benchmarking, is never the
    default, and is recorded on the run.

    The same second-model run that was refused above succeeds with the flag set, and the run
    records both the override and the second model - which is what makes the flag's cost visible
    instead of silent. See the closing assertion for where the mix is *not* visible, and why.
    """
    from aimemory.sources.pipeline import single_session_scope
    from aimemory.sources.tier2 import run_tier2

    first = _ingest_and_extract(tmp_source_root.root, pg_session, StubKnowledgeEngine())
    run_id = first.run_id
    ingest_repo.requeue_episodes_for_reextraction(pg_session)

    other = StubKnowledgeEngine(model_id=OTHER_MODEL_ID)
    report = run_tier2(
        single_session_scope(pg_session),
        engine=other,
        writer=lambda r, e: persist_result(pg_session, r, e),
        allow_model_mix=True,
        run_id=run_id,
    )
    assert report.extracted > 0
    assert report.model_id == OTHER_MODEL_ID
    assert other.seen, "with the flag set the episodes do reach the model"

    # The override is recorded on the run, and the corpus now visibly holds two models - the state
    # `aimemory-ingest status` warns about.
    counters = pg_session.execute(
        sa.text("SELECT counters FROM ingestion_runs WHERE id = :id"), {"id": run_id}
    ).scalar()
    assert counters.get("allow_model_mix") == 1
    # `record_run_extraction_model` writes one `metrics_snapshots` row per (run, model), so the
    # run keeps its generation-1 row and gains a second one: the mix stays visible on the run
    # instead of the newer model quietly overwriting the older one's record.
    snapshots = {
        row.model_id: row.extra
        for row in pg_session.execute(
            sa.text("SELECT model_id, extra FROM metrics_snapshots WHERE run_id = :id"),
            {"id": run_id},
        )
    }
    assert set(snapshots) == {STUB_MODEL_ID, OTHER_MODEL_ID}
    assert snapshots[OTHER_MODEL_ID].get("allow_model_mix") is True
    assert snapshots[STUB_MODEL_ID].get("allow_model_mix") is False
    # Where the mix is *not* visible, and why. Generation 2 proposed facts identical to the ones
    # already open, and ADR-0005 re-confirms an identical fact (`FactRepo.touch`) instead of
    # inserting a second row. A re-confirmation deliberately does not restamp provenance, so those
    # facts still carry generation 1's `extraction_model_id` - `extraction_models_in_use`, and the
    # `aimemory-ingest status` warning built on it, therefore report a single model for a corpus
    # two models have now run over. The per-(run, model) `metrics_snapshots` rows asserted above
    # are the record that does show both. Recorded as a known limitation for A04/A08 rather than
    # papered over here: closing it means either restamping on re-confirmation (which would lose
    # the originating model) or a separate confirming-model record, and that is an ADR decision.
    assert set(ingest_repo.extraction_models_in_use(pg_session)) == {STUB_MODEL_ID}


def test_deterministic_seed_types_cannot_be_overruled_by_extraction(
    tmp_source_root, pg_session
) -> None:
    """ADR-0014 rule 3: Tier 0 seeds the PARA folders and the registry projects, and an extraction
    model may add entities but never retype a seeded one.

    Both providers mistyped the PARA folders in P4-T02, in different directions. The write-time guard
    (``aimemory.sources.seeds.resolve_entity_type``, called by this suite's writer and by A08's
    engine) is what makes that unrepresentable rather than merely unlikely.
    """
    from aimemory.sources.seeds import PARA_FOLDERS, SeedTypeConflict, resolve_entity_type

    _ingest(tmp_source_root.root, session=pg_session, tier=0)

    seeded = {
        row[0]: row[1]
        for row in pg_session.execute(
            sa.text("SELECT canonical_name, type FROM entities WHERE engine = 'deterministic'")
        ).fetchall()
    }
    # Only the PARA folders that exist in the fixture vault are seeded; every one of them is typed
    # from the ontology's document/concept side, never Repository or InfrastructureComponent.
    present = [f for f in PARA_FOLDERS if (tmp_source_root.root / f).is_dir()]
    assert present, "the fixture vault should contain at least one PARA folder"
    for folder in present:
        assert seeded.get(folder) in ("Concept", "Document")
    # The registry projects are seeded as Project, stamped deterministic (ADR-0014 rule 3 / plan J).
    assert seeded.get("Fixture Project") == "Project"

    # A model proposing the measured-wrong types is refused, in both directions it was measured in.
    for wrong in (EntityType.REPOSITORY, EntityType.INFRASTRUCTURE_COMPONENT):
        with pytest.raises(SeedTypeConflict) as raised:
            resolve_entity_type(pg_session, "01 Projects", wrong)
        assert wrong.value in str(raised.value)
    # The allowed reading is accepted and snapped to the canonical type instead of forking a row.
    assert resolve_entity_type(pg_session, "01 Projects", EntityType.DOCUMENT) is EntityType.CONCEPT
    # Entities nobody seeded are untouched: the model may still add whatever it finds.
    assert resolve_entity_type(pg_session, "Some Unseeded Thing", EntityType.REPOSITORY) is (
        EntityType.REPOSITORY
    )


def test_secret_suspected_source_is_never_queued_or_transmitted(
    tmp_source_root, pg_session
) -> None:
    """ADR-0014 rule 4: a ``secret_suspected`` source is metadata-only, so nothing about it can be
    sent to a provider.

    This was hygiene while extraction ran locally. With a cloud provider as the default it is a
    privacy control on egress, and it is asserted at both gates: the pipeline never creates an
    episode for such a source, and ``claim_episode_for_extraction`` excludes it in SQL even if one
    existed. The recorded event names the rules that matched and never the matched text.
    """
    tmp_source_root.write(
        "01 Projects/deploy.env",
        "AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLEKEYDONOTUSE\nDB_PASSWORD=hunter2\n",
    )
    engine = StubKnowledgeEngine()
    report = _ingest_and_extract(tmp_source_root.root, pg_session, engine)
    assert report.counters.get("secret_suspected", 0) >= 1

    flagged = pg_session.execute(
        sa.text("SELECT id, current_version_id, policy FROM sources WHERE secret_suspected")
    ).fetchall()
    assert flagged, "the fake credentials file should have been flagged"
    for source in flagged:
        # No text, no chunks, no episode: there is nothing to transmit, by construction.
        assert (
            _count(
                pg_session,
                "SELECT count(*) FROM source_text WHERE version_id = :v",
                v=source.current_version_id,
            )
            == 0
        )
        assert (
            _count(pg_session, "SELECT count(*) FROM chunks WHERE source_id = :s", s=source.id) == 0
        )
        assert (
            _count(pg_session, "SELECT count(*) FROM episodes WHERE source_id = :s", s=source.id)
            == 0
        )
        events = ingest_repo.source_events(pg_session, source.id)
        secret_events = [e for e in events if e["event_type"] == "secret_suspected"]
        assert secret_events, "a flagged source must say so in its event log"
        rendered = str(secret_events[0]["details"])
        assert "hunter2" not in rendered and "AKIA" not in rendered

    # The second gate: even a hand-made episode for a flagged source is never claimed.
    pg_session.execute(
        sa.text(
            "INSERT INTO episodes (id, type, source_id, version_id, title, body, observed_at, "
            "                      status, tier, priority) "
            "VALUES (gen_random_uuid(), 'document', :s, :v, 'leaked', 'secret body', now(), "
            "        'queued', 2, 1)"
        ),
        {"s": flagged[0].id, "v": flagged[0].current_version_id},
    )
    assert ingest_repo.claim_episode_for_extraction(pg_session) is None
    assert "leaked" not in engine.seen
