"""P5-T01: repository logic against a real PostgreSQL, with every test wrapped in a transaction
that is rolled back at teardown so no test leaves data behind.

Owner: A04. These need a reachable ``DATABASE_URL`` (the compose ``postgres`` service, or a
loopback-published one for host runs) - if it cannot be reached the whole module is skipped rather
than failing, since P5-T01's own migration state (schema at head) is also a precondition these tests
assume but do not themselves create (see ``tests/integration/test_migrations.py`` for that).

A12 has since landed shared ``db_engine``/``pg_session`` fixtures in ``tests/conftest.py``; the two
fixtures below are now thin wrappers over those (kept under their original names, ``_require_postgres``
and ``session``, so no test body below needed to change - see the P2-T04 HANDOFF).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import sqlalchemy as sa
from aimemory.domain.enums import (
    ArtifactType,
    ChangeType,
    EntityType,
    EpisodeType,
    JobStage,
    JobState,
    ObjectType,
    Predicate,
    RunStatus,
    RunTrigger,
    SourceKind,
    StoragePolicy,
    Track,
)
from aimemory.domain.models import (
    ArtifactEntity,
    Chunk,
    Embedding,
    Entity,
    Episode,
    ExtractionReview,
    Fact,
    IngestionJob,
    IngestionRun,
    KnowledgeArtifact,
    McpAuditLog,
    MetricsSnapshot,
    Project,
    RetrievalLog,
    RunRequest,
    ServiceStat,
    Source,
    SourceVersion,
)
from aimemory.domain.provenance import Provenance
from aimemory.persistence.repositories import (
    ArtifactRepo,
    AuditRepo,
    ChunkRepo,
    EmbeddingRepo,
    EntityRepo,
    EpisodeRepo,
    FactRepo,
    JobRepo,
    MetricsRepo,
    ProjectRepo,
    RunRepo,
    SourceRepo,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

pytestmark = pytest.mark.usefixtures("_require_postgres")

NOW = datetime.now(UTC)
DEVICE_ID = "local-development-machine"


@pytest.fixture()
def _require_postgres(postgres_available: bool) -> None:
    """``postgres_available`` (tests/conftest.py) already skips when unreachable; this name is kept
    so the ``pytestmark`` line above did not need to change."""


@pytest.fixture()
def session(pg_session: Session) -> Session:
    """Renamed wrapper over the shared ``pg_session`` fixture (tests/conftest.py) - every test below
    keeps using the parameter name ``session`` unchanged."""
    return pg_session


@pytest.fixture()
def project(session: Session) -> Project:
    return ProjectRepo(session).upsert(Project(id="t-persist-proj", name="Persistence Test", track=Track.RESEARCH))


@pytest.fixture()
def source_root(session: Session, project: Project) -> str:
    session.execute(
        sa.text(
            "INSERT INTO source_roots (root_id, scheme, label, container_path, device_id, "
            "default_project_id) VALUES ('t-persist-root', 'vault', 't-persist-root', "
            "'/sources/vault', :device_id, :project_id)"
        ),
        {"device_id": DEVICE_ID, "project_id": project.id},
    )
    return "t-persist-root"


@pytest.fixture()
def source(session: Session, project: Project, source_root: str) -> Source:
    return SourceRepo(session).upsert_source(
        Source(
            id=uuid4(),
            uri="vault://t-persist/file.md",
            root_id=source_root,
            relative_path="t-persist/file.md",
            project_id=project.id,
            kind=SourceKind.FILE,
            policy=StoragePolicy.INDEX_CONTENT,
        )
    )


@pytest.fixture()
def version(session: Session, source: Source) -> SourceVersion:
    saved, _created = SourceRepo(session).record_version(
        SourceVersion(
            id=uuid4(),
            source_id=source.id,
            content_hash="sha256:" + "a" * 64,
            size_bytes=42,
            observed_at=NOW,
            change_type=ChangeType.NEW,
        )
    )
    return saved


@pytest.fixture()
def episode(session: Session, project: Project, source: Source, version: SourceVersion) -> Episode:
    return EpisodeRepo(session).create(
        Episode(
            id=uuid4(),
            type=EpisodeType.DOCUMENT,
            source_id=source.id,
            version_id=version.id,
            project_id=project.id,
            title="Test episode",
            observed_at=NOW,
        )
    )


def _prov(**overrides: object) -> Provenance:
    base = {"device_id": DEVICE_ID, "observed_at": NOW}
    base.update(overrides)
    return Provenance(**base)  # type: ignore[arg-type]


# ---- ProjectRepo -----------------------------------------------------------------------------


def test_project_upsert_is_idempotent(session: Session) -> None:
    repo = ProjectRepo(session)
    p = Project(id="t-idempotent-proj", name="A", track=Track.BUSINESS)
    first = repo.upsert(p)
    second = repo.upsert(p.model_copy(update={"name": "B"}))
    assert first.id == second.id
    assert repo.get("t-idempotent-proj").name == "B"


# ---- SourceRepo ------------------------------------------------------------------------------


def test_record_version_dedupes_identical_content_hash(session: Session, source: Source) -> None:
    repo = SourceRepo(session)
    v1, created1 = repo.record_version(
        SourceVersion(
            id=uuid4(), source_id=source.id, content_hash="sha256:" + "b" * 64, size_bytes=1,
            observed_at=NOW, change_type=ChangeType.NEW,
        )
    )
    v2, created2 = repo.record_version(
        SourceVersion(
            id=uuid4(), source_id=source.id, content_hash="sha256:" + "b" * 64, size_bytes=1,
            observed_at=NOW, change_type=ChangeType.NEW,
        )
    )
    assert created1 is True
    assert created2 is False
    assert v1.id == v2.id


def test_mark_deleted_propagates_to_facts_and_artifacts(
    session: Session, project: Project, source: Source, episode: Episode
) -> None:
    entity = EntityRepo(session).upsert(
        Entity(id=uuid4(), type=EntityType.PROJECT, canonical_name="X", normalized_name="x", project_id=project.id)
    )
    fact = FactRepo(session).insert(
        Fact(
            id=uuid4(), subject_entity_id=entity.id, predicate=Predicate.HAS_STATUS, object_value="active",
            statement="X is active", valid_from=NOW, observed_at=NOW, project_id=project.id,
            provenance=_prov(source_id=source.id, episode_id=episode.id),
        )
    )
    SourceRepo(session).mark_deleted(source.id)
    row = session.execute(sa.text("SELECT source_status FROM facts WHERE id = :id"), {"id": fact.id}).first()
    assert row is not None and row.source_status == "deleted"


def test_mark_moved_keeps_id_and_records_moved_from(session: Session, source: Source) -> None:
    moved = SourceRepo(session).mark_moved(source.id, "vault://t-persist/renamed.md", source.uri)
    assert moved.id == source.id
    assert moved.uri == "vault://t-persist/renamed.md"
    assert moved.moved_from_uri == source.uri


# ---- ChunkRepo / EmbeddingRepo ----------------------------------------------------------------


def test_chunk_bulk_insert_and_fetch(session: Session, source: Source, version: SourceVersion) -> None:
    chunk = Chunk(
        id=uuid4(), version_id=version.id, source_id=source.id, ordinal=0, text="hello",
        text_hash="sha256:" + "c" * 64, char_start=0, char_end=5, token_count=1,
    )
    assert ChunkRepo(session).bulk_insert([chunk]) == 1
    fetched = ChunkRepo(session).get_by_version(version.id)
    assert len(fetched) == 1 and fetched[0].id == chunk.id


def test_embedding_reuse_key_is_idempotent(session: Session, source: Source, version: SourceVersion) -> None:
    chunk = Chunk(
        id=uuid4(), version_id=version.id, source_id=source.id, ordinal=0, text="hello",
        text_hash="sha256:" + "d" * 64, char_start=0, char_end=5, token_count=1,
    )
    ChunkRepo(session).bulk_insert([chunk])
    repo = EmbeddingRepo(session)
    emb = Embedding(
        id=uuid4(), object_type=ObjectType.CHUNK, object_id=chunk.id, text_hash=chunk.text_hash,
        model_id="minilm-l6-v2-384", dimension=384, vector=[0.1] * 384,
    )
    assert repo.bulk_insert([emb]) == 1
    assert repo.bulk_insert([emb]) == 0  # ON CONFLICT (text_hash, model_id) DO NOTHING
    got = repo.get_by_hash(chunk.text_hash, "minilm-l6-v2-384")
    assert got is not None
    assert len(got.vector) == 384


# ---- EpisodeRepo -----------------------------------------------------------------------------


def test_claim_next_and_mark_failed(session: Session, episode: Episode) -> None:
    repo = EpisodeRepo(session)
    claimed = repo.claim_next()
    assert claimed is not None and claimed.id == episode.id and claimed.status.value == "running"
    failed = repo.mark_failed(episode.id, "boom")
    assert failed.status.value == "failed"
    assert failed.attempts == 1
    assert failed.error == "boom"


# ---- EntityRepo / FactRepo (ADR-0005 rule 1: the functional-predicate backstop) ---------------


def test_uq_facts_functional_current_blocks_a_second_open_functional_fact(
    session: Session, project: Project, episode: Episode
) -> None:
    entity = EntityRepo(session).upsert(
        Entity(id=uuid4(), type=EntityType.PROJECT, canonical_name="Y", normalized_name="y", project_id=project.id)
    )
    repo = FactRepo(session)
    repo.insert(
        Fact(
            id=uuid4(), subject_entity_id=entity.id, predicate=Predicate.HAS_STATUS, object_value="active",
            statement="Y is active", valid_from=NOW, observed_at=NOW, project_id=project.id,
            provenance=_prov(episode_id=episode.id),
        )
    )
    session.flush()
    with pytest.raises(IntegrityError):
        repo.insert(
            Fact(
                id=uuid4(), subject_entity_id=entity.id, predicate=Predicate.HAS_STATUS, object_value="paused",
                statement="Y is paused", valid_from=NOW, observed_at=NOW, project_id=project.id,
                provenance=_prov(episode_id=episode.id),
            )
        )
    session.rollback()


def test_close_fact_is_idempotent(session: Session, project: Project, episode: Episode) -> None:
    entity = EntityRepo(session).upsert(
        Entity(id=uuid4(), type=EntityType.PROJECT, canonical_name="Z", normalized_name="z", project_id=project.id)
    )
    fact = FactRepo(session).insert(
        Fact(
            id=uuid4(), subject_entity_id=entity.id, predicate=Predicate.HAS_STATUS, object_value="active",
            statement="Z is active", valid_from=NOW, observed_at=NOW, project_id=project.id,
            provenance=_prov(episode_id=episode.id),
        )
    )
    repo = FactRepo(session)
    first = repo.close_fact(fact.id, NOW, episode.id)
    second = repo.close_fact(fact.id, NOW, episode.id)
    assert first.valid_to == second.valid_to
    assert first.status.value == "historical"


def test_insert_superseding_links_the_chain(session: Session, project: Project, episode: Episode) -> None:
    entity = EntityRepo(session).upsert(
        Entity(id=uuid4(), type=EntityType.PROJECT, canonical_name="W", normalized_name="w", project_id=project.id)
    )
    repo = FactRepo(session)
    old = repo.insert(
        Fact(
            id=uuid4(), subject_entity_id=entity.id, predicate=Predicate.HAS_STATUS, object_value="active",
            statement="W is active", valid_from=NOW, observed_at=NOW, project_id=project.id,
            provenance=_prov(episode_id=episode.id),
        )
    )
    repo.close_fact(old.id, NOW, episode.id)
    new_fact = Fact(
        id=uuid4(), subject_entity_id=entity.id, predicate=Predicate.HAS_STATUS, object_value="paused",
        statement="W is paused", valid_from=NOW, observed_at=NOW, project_id=project.id,
        provenance=_prov(episode_id=episode.id),
    )
    saved = repo.insert_superseding(new_fact, old.id)
    assert saved.supersedes_fact_id == old.id


# ---- ArtifactRepo ------------------------------------------------------------------------------


def test_artifact_insert_and_supersede(session: Session, project: Project, episode: Episode) -> None:
    repo = ArtifactRepo(session)
    entity = EntityRepo(session).upsert(
        Entity(id=uuid4(), type=EntityType.TECHNOLOGY, canonical_name="Neo4j", normalized_name="neo4j")
    )
    old = repo.insert(
        KnowledgeArtifact(
            id=uuid4(), type=ArtifactType.DECISION, title="A", body="body", project_id=project.id,
            valid_from=NOW, provenance=_prov(episode_id=episode.id),
        )
    )
    new = repo.insert(
        KnowledgeArtifact(
            id=uuid4(), type=ArtifactType.DECISION, title="B", body="body", project_id=project.id,
            valid_from=NOW, supersedes_id=old.id, provenance=_prov(episode_id=episode.id),
        )
    )
    repo.supersede(old.id, new.id, NOW)
    refreshed = repo.get(old.id)
    assert refreshed is not None
    assert refreshed.current_status.value == "superseded"
    assert refreshed.superseded_by_id == new.id
    repo.link_entity(ArtifactEntity(artifact_id=new.id, entity_id=entity.id, role="about"))
    repo.link_entity(ArtifactEntity(artifact_id=new.id, entity_id=entity.id, role="about"))  # idempotent


# ---- RunRepo / JobRepo -------------------------------------------------------------------------


def test_job_upsert_is_idempotent_on_version_and_stage(session: Session, version: SourceVersion) -> None:
    run = RunRepo(session).create(IngestionRun(id=uuid4(), trigger=RunTrigger.TEST))
    repo = JobRepo(session)
    job1 = repo.upsert(
        IngestionJob(id=uuid4(), run_id=run.id, version_id=version.id, stage=JobStage.CHUNK, state=JobState.PENDING)
    )
    job2 = repo.upsert(
        IngestionJob(id=uuid4(), run_id=run.id, version_id=version.id, stage=JobStage.CHUNK, state=JobState.DONE)
    )
    assert job1.id == job2.id
    assert job2.state.value == "done"
    assert job2.attempts == job1.attempts + 1


def test_run_finish_records_counters(session: Session) -> None:
    repo = RunRepo(session)
    run = repo.create(IngestionRun(id=uuid4(), trigger=RunTrigger.TEST))
    finished = repo.finish(run.id, status=RunStatus.COMPLETED, counters={"discovered": 3})
    assert finished.status.value == "completed"
    assert finished.counters == {"discovered": 3}


# ---- AuditRepo / MetricsRepo --------------------------------------------------------------------


def test_audit_and_metrics_round_trip(session: Session) -> None:
    audit = AuditRepo(session).log_mcp_call(
        McpAuditLog(id=uuid4(), tool="memory.search", kind="read", arguments={"q": "x"})
    )
    assert audit.arguments == {"q": "x"}

    rlog = AuditRepo(session).log_retrieval(
        RetrievalLog(id=uuid4(), query_text="q", query_hash="h", params={"a": 1.0})
    )
    assert rlog.limit == 10
    assert rlog.params == {"a": 1.0}

    metrics = MetricsRepo(session)
    snap = metrics.insert_snapshot(MetricsSnapshot(id=uuid4(), scope="eval", extra={"x": 1.0}))
    assert metrics.latest("eval").id == snap.id

    review = metrics.add_review(
        ExtractionReview(id=uuid4(), object_type=ObjectType.FACT, object_id=uuid4(), verdict="accept")
    )
    assert review.verdict.value == "accept"

    rr = metrics.enqueue_run_request(RunRequest(id=uuid4(), options={"root": "vault"}))
    claimed = metrics.claim_next_run_request()
    assert claimed is not None and claimed.id == rr.id

    stat = metrics.record_service_stat(ServiceStat(id=uuid4(), service="ingestion", details={"k": "v"}))
    assert stat.details == {"k": "v"}
