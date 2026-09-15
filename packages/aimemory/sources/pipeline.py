"""The ingestion state machine (A07a, P6-T03) - plan section L, ADR-0006 tiering.

One class, :class:`IngestionPipeline`, drives every scan:

``discover -> classify -> fingerprint -> diff -> extract metadata -> extract text -> chunk ->
embed -> episode`` (Tier 0/1), with the Tier 2 stages (``extract_knowledge`` onward) queued for
:mod:`aimemory.sources.tier2`.

Design rules this file exists to enforce
----------------------------------------
* **Resumability is a database property, not a memory property.** Every stage writes an
  ``ingestion_jobs`` row keyed ``(version_id, stage)`` - a key the database enforces - and every data
  write is an upsert with a deterministic id or an ``ON CONFLICT`` clause. A killed process is
  recovered by re-running the same command: ``JobRepo.requeue_stuck()`` re-queues the ``running``
  rows, finished stages are skipped, and nothing is written twice.
* **One transaction per source.** The unit of work is a file, so an interruption loses at most the
  file in flight. The pipeline asks its :class:`SessionScope` for a session per file; in tests that
  scope hands back one caller-owned session that is rolled back at the end, in production it is
  :class:`aimemory.persistence.db.Database`.
* **Tiers are ceilings, not steps.** ``--tier 0`` catalogs sources and seeds the project registry,
  ``--tier 1`` adds text/chunks/embeddings, ``--tier 2`` also queues (and, when a knowledge engine is
  available, runs) LLM extraction.
* **Secrets never reach an LLM.** A ``secret_suspected`` source is metadata only: no ``source_text``,
  no chunks, no episode - so there is nothing for Tier 2 to send anywhere. Under ADR-0012 one of the
  two providers is AWS Bedrock, which makes this a privacy control rather than hygiene.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from ..chunking import get_chunker
from ..common.config import Settings, get_settings
from ..common.ids import deterministic_id, new_id, normalize_name
from ..common.logging import get_logger
from ..common.time import utc_now
from ..domain.enums import (
    ChangeType,
    EpisodeStatus,
    EpisodeType,
    JobStage,
    JobState,
    ObjectType,
    Origin,
    RunStatus,
    RunTrigger,
    SourceEventType,
    SourceKind,
    SourceStatus,
    StoragePolicy,
    Tier,
    Trust,
)
from ..domain.models import (
    Chunk,
    Embedding,
    Episode,
    IngestionJob,
    IngestionRun,
    Source,
    SourceEvent,
    SourceText,
    SourceVersion,
)
from ..domain.ports import EmbeddingProvider, ExtractedText, chunk_from_draft
from ..extractors import get_extractor
from ..persistence import ingest_repo
from ..persistence.repositories import (
    ChunkRepo,
    EmbeddingRepo,
    EpisodeRepo,
    JobRepo,
    RunRepo,
    SourceRepo,
)
from . import secrets as secret_scanner
from .diff import ChangeDecision, KnownSource, Observed, classify_changes, summarize
from .discovery import WalkStats, walk_root
from .fingerprint import Fingerprint, GitHead, fingerprint_file, read_git_head
from .policies import resolve_policy
from .roots import RootContext

__all__ = ["IngestionPipeline", "RunReport", "SessionScope", "single_session_scope"]

logger = get_logger(__name__)

#: Chunk target. MiniLM-L6-v2 has ``max_seq=256``, so chunks aim at ~200 tokens (ports.ChunkDraft).
CHUNK_MAX_TOKENS = 200


class SessionScope(Protocol):
    """Anything that can hand out a :class:`~sqlalchemy.orm.Session` as a context manager.

    :class:`aimemory.persistence.db.Database` satisfies it as-is (commit on clean exit, rollback on
    error); :func:`single_session_scope` wraps a caller-owned session for tests.
    """

    def session(self) -> AbstractContextManager[Session]:
        ...


class _SingleSessionScope:
    """Hands back the same session every time, inside a SAVEPOINT - the caller owns the transaction.

    The savepoint matters: :class:`aimemory.persistence.db.Database` gives every unit of work its own
    transaction, so a failed stage rolls back that stage and nothing else. A test session is a single
    long transaction, and in PostgreSQL *any* failed statement aborts the whole transaction
    ("current transaction is aborted, commands ignored") - which would turn one bad file into a
    cascade of failures that production would never see. ``begin_nested()`` reproduces the production
    boundary exactly, without committing anything the caller did not ask for.
    """

    def __init__(self, session: Session, *, flush: bool = True) -> None:
        self._session = session
        self._flush = flush

    @contextmanager
    def session(self) -> Iterator[Session]:
        nested = self._session.begin_nested()
        try:
            yield self._session
            if self._flush:
                self._session.flush()
        except Exception:
            if nested.is_active:
                nested.rollback()
            raise
        else:
            if nested.is_active:
                nested.commit()  # releases the savepoint; the outer transaction stays open


def single_session_scope(session: Session, *, flush: bool = True) -> SessionScope:
    """A :class:`SessionScope` over one existing session (``pg_session`` in the test suite)."""
    return _SingleSessionScope(session, flush=flush)


@dataclass
class RunReport:
    """Everything one root's scan did. Returned by :meth:`IngestionPipeline.run_root`."""

    root_id: str
    tier: Tier
    dry_run: bool = False
    run_id: UUID | None = None
    counters: dict[str, int] = field(default_factory=dict)
    decisions: list[ChangeDecision] = field(default_factory=list)
    walk: WalkStats = field(default_factory=WalkStats)
    errors: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    #: The :class:`aimemory.sources.tier2.Tier2Report` of the same run, when Tier 2 was drained in
    #: the same call (``run_ingestion(engine=...)``). ``None`` for a Tier 0/1 scan.
    tier2: Any = None

    def count(self, key: str, delta: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + delta

    @property
    def changed(self) -> int:
        return sum(
            self.counters.get(k, 0) for k in ("new", "modified", "moved", "deleted", "duplicate")
        )


class _StageSkipped(Exception):  # control flow, not an error condition
    """Raised inside a stage body to record ``skipped`` instead of ``done``."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StageOutcome(StrEnum):
    """What :meth:`IngestionPipeline._stage` did.

    ``ALREADY_DONE`` is the resume path: this ``(version_id, stage)`` finished in an earlier,
    possibly killed, run, so the body is not executed again and the caller must load whatever the
    body would have produced from the database instead.
    """

    DONE = "done"
    ALREADY_DONE = "already_done"
    SKIPPED = "skipped"
    FAILED = "failed"

    @property
    def ok(self) -> bool:
        return self in (StageOutcome.DONE, StageOutcome.ALREADY_DONE)


class IngestionPipeline:
    """Runs one or more roots through the plan section L state machine."""

    def __init__(
        self,
        scope: SessionScope,
        *,
        settings: Settings | None = None,
        embedder: EmbeddingProvider | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._scope = scope
        self._settings = settings or get_settings()
        self._embedder = embedder
        self._clock = clock
        self._embedding_model_key: str | None = None
        self._project_ids: set[str] = set()
        self._alias_map: dict[str, str] = {}

    # ------------------------------------------------------------------------------------ public

    def sync_roots(self, roots: Sequence[Any]) -> int:
        """Write ``config/source-roots.yaml`` into ``source_roots`` (A04 assigned this to A07a)."""
        with self._scope.session() as session:
            return ingest_repo.sync_source_roots(session, roots)

    def run_root(
        self,
        ctx: RootContext,
        *,
        tier: Tier = Tier.KNOWLEDGE,
        dry_run: bool = False,
        trigger: RunTrigger = RunTrigger.CLI,
        requested_by: str | None = None,
        subpath: str | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> RunReport:
        """Scan one root end to end. Idempotent: running it twice changes nothing the second time."""
        report = RunReport(
            root_id=ctx.root_id, tier=tier, dry_run=dry_run, started_at=self._clock()
        )

        run = None
        if not dry_run:
            with self._scope.session() as session:
                run = RunRepo(session).create(
                    IngestionRun(
                        id=new_id(),
                        root_id=ctx.root_id,
                        tier=tier,
                        trigger=trigger,
                        requested_by=requested_by,
                        status=RunStatus.RUNNING,
                        started_at=report.started_at or utc_now(),
                    )
                )
                report.run_id = run.id
                requeued = JobRepo(session).requeue_stuck()
                if requeued:
                    report.count("jobs_requeued", requeued)
                self._project_ids = ingest_repo.known_project_ids(session)
                self._alias_map = ingest_repo.project_alias_map(session)

        try:
            # ---- Tier 0: the deterministic project registry (my-vault only, ADR-0006) ----------
            if ctx.root.registry_role == "bootstrap" and not dry_run:
                from .registry import seed_registry  # local import: registry imports nothing heavy
                from .seeds import seed_deterministic_entities

                with self._scope.session() as session:
                    seeded = seed_registry(session, ctx, run_id=report.run_id)
                for key, value in seeded.items():
                    report.count(key, value)
                with self._scope.session() as session:
                    self._project_ids = ingest_repo.known_project_ids(session)
                    self._alias_map = ingest_repo.project_alias_map(session)
                    if ctx.root.default_project_id:
                        ingest_repo.attach_root_project(
                            session, ctx.root_id, ctx.root.default_project_id
                        )
                # ADR-0014 rule 3: the PARA folders and the registry projects are typed here, by
                # construction, so no extraction model gets a vote on them later.
                with self._scope.session() as session:
                    for key, value in seed_deterministic_entities(
                        session, ctx, now=self._clock()
                    ).items():
                        report.count(key, value)

            # ---- discover + fingerprint --------------------------------------------------------
            observed, fingerprints = self._discover(ctx, report, subpath=subpath)

            # ---- diff (needs the database even for a dry run) -----------------------------------
            with self._scope.session() as session:
                known = [
                    KnownSource(
                        source_id=row.source_id,
                        uri=row.uri,
                        relative_path=row.relative_path,
                        content_hash=row.content_hash,
                        status=row.status,
                        version_id=row.version_id,
                    )
                    for row in ingest_repo.known_sources_for_root(session, ctx.root_id)
                ]
            decisions = classify_changes(observed, known, full_scan=subpath is None)
            report.decisions = decisions
            for key, value in summarize(decisions).items():
                report.counters[key] = value

            if dry_run:
                # The walk numbers are the whole point of a dry run ("what would you have looked
                # at, and what did you prune"), so they are folded in before the early return as
                # well as at the end of a real scan.
                for key, value in report.walk.as_counters().items():
                    report.counters[key] = value
                report.finished_at = self._clock()
                return report

            git_head = read_git_head(ctx.base_path) if ctx.is_repository else GitHead(None, None)
            total = len(decisions)
            unchanged_ids: list[UUID] = []
            for index, decision in enumerate(decisions, start=1):
                if progress is not None and (index % 25 == 0 or index == total):
                    progress(index, total, ctx.root_id)
                if decision.change_type is ChangeType.UNCHANGED:
                    if decision.known is not None:
                        unchanged_ids.append(decision.known.source_id)
                    if decision.restored:
                        self._restore(decision, report)
                    continue
                try:
                    self._process(ctx, decision, fingerprints, report, tier, git_head)
                except Exception as exc:  # noqa: BLE001 - one bad file must not kill the scan
                    message = f"{type(exc).__name__}: {exc}"
                    report.errors.append(f"{decision.uri}: {message}")
                    report.count("errors")
                    logger.warning(
                        "ingestion.source_failed", uri=decision.uri, error=message
                    )

            if unchanged_ids:
                now = self._clock()
                for batch in _batched(unchanged_ids, 500):
                    with self._scope.session() as session:
                        ingest_repo.touch_last_seen(session, batch, now)

            for key, value in report.walk.as_counters().items():
                report.counters[key] = value
            report.finished_at = self._clock()
            if run is not None:
                # A failed *stage* marks its job row failed and is reported here; it does not fail
                # the run (plan section L: "vectors intact; reprocess --failed").
                with self._scope.session() as session:
                    RunRepo(session).finish(
                        run.id,
                        status=RunStatus.COMPLETED,
                        counters=report.counters,
                        error="; ".join(report.errors[:5]) or None,
                    )
            return report
        except Exception as exc:
            report.finished_at = self._clock()
            if run is not None:
                with self._scope.session() as session:
                    RunRepo(session).finish(
                        run.id,
                        status=RunStatus.FAILED,
                        counters=report.counters,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            raise

    # -------------------------------------------------------------------------------- discovery

    def _discover(
        self, ctx: RootContext, report: RunReport, *, subpath: str | None
    ) -> tuple[list[Observed], dict[str, Fingerprint]]:
        """Walk, fingerprint, and build the ``Observed`` list the diff needs."""
        observed: list[Observed] = []
        fingerprints: dict[str, Fingerprint] = {}
        max_read = self._settings.ingest.max_text_bytes
        for found in walk_root(ctx, stats=report.walk, subpath=subpath):
            try:
                fingerprint = fingerprint_file(
                    found.path,
                    size_bytes=found.size_bytes,
                    mtime=found.mtime,
                    max_read_bytes=max_read,
                )
            except OSError:
                report.walk.unreadable += 1
                continue
            uri = ctx.uri_for(found.relative_path)
            fingerprints[uri] = fingerprint
            observed.append(
                Observed(
                    uri=uri,
                    relative_path=found.relative_path,
                    content_hash=fingerprint.content_hash,
                    size_bytes=fingerprint.size_bytes,
                    mtime=fingerprint.mtime,
                )
            )
        return observed, fingerprints

    # ------------------------------------------------------------------------------- processing

    def _restore(self, decision: ChangeDecision, report: RunReport) -> None:
        """A deleted URI came back with identical content (ADR-0005 rule 4: never a second row)."""
        assert decision.known is not None
        with self._scope.session() as session:
            SourceRepo(session).restore(decision.known.source_id)
            SourceRepo(session).record_event(
                SourceEvent(
                    id=new_id(),
                    source_id=decision.known.source_id,
                    version_id=decision.known.version_id,
                    event_type=SourceEventType.RESTORED,
                    at=self._clock(),
                    run_id=report.run_id,
                    details={"uri": decision.uri},
                )
            )
        report.count("restored")

    def _process(
        self,
        ctx: RootContext,
        decision: ChangeDecision,
        fingerprints: dict[str, Fingerprint],
        report: RunReport,
        tier: Tier,
        git_head: GitHead,
    ) -> None:
        if decision.change_type is ChangeType.DELETED:
            self._process_deleted(decision, report)
            return
        if decision.change_type is ChangeType.MOVED:
            self._process_moved(ctx, decision, report)
            return

        fingerprint = fingerprints[decision.uri]
        classification = self._classify(ctx, decision.relative_path, fingerprint)

        with self._scope.session() as session:
            sources = SourceRepo(session)
            existing = sources.get_by_uri(decision.uri)
            now = self._clock()
            source = sources.upsert_source(
                Source(
                    id=existing.id if existing else new_id(),
                    uri=decision.uri,
                    root_id=ctx.root_id,
                    relative_path=decision.relative_path,
                    project_id=classification.project_id,
                    kind=SourceKind.FILE,
                    media_type=classification.media_type,
                    policy=classification.policy,
                    policy_reason=classification.reason,
                    status=SourceStatus.ACTIVE,
                    moved_from_uri=existing.moved_from_uri if existing else None,
                    current_version_id=existing.current_version_id if existing else None,
                    secret_suspected=classification.secret_suspected,
                    origin=classification.origin,
                    trust=classification.trust,
                    first_seen_at=existing.first_seen_at if existing else now,
                    last_seen_at=now,
                )
            )
            version, created = sources.record_version(
                SourceVersion(
                    id=new_id(),
                    source_id=source.id,
                    content_hash=fingerprint.content_hash,
                    size_bytes=fingerprint.size_bytes,
                    mtime=fingerprint.mtime,
                    git_commit=git_head.commit,
                    git_branch=git_head.branch,
                    observed_at=now,
                    ingestion_run_id=report.run_id,
                    is_current=True,
                    change_type=decision.change_type,
                )
            )
            sources.set_current_version(source.id, version.id)
            if created:
                report.count("versions_created")
            sources.record_event(
                SourceEvent(
                    id=new_id(),
                    source_id=source.id,
                    version_id=version.id,
                    event_type=_EVENT_FOR_CHANGE[decision.change_type],
                    at=now,
                    run_id=report.run_id,
                    details=_event_details(decision, classification),
                )
            )
            if classification.secret_suspected:
                sources.record_event(
                    SourceEvent(
                        id=new_id(),
                        source_id=source.id,
                        version_id=version.id,
                        event_type=SourceEventType.SECRET_SUSPECTED,
                        at=now,
                        run_id=report.run_id,
                        # Rule names only - never the matched text, never the content (plan §K).
                        details={"rules": ",".join(classification.secret_rules)},
                    )
                )
                report.count("secret_suspected")
            if decision.change_type is ChangeType.MODIFIED:
                touched = ingest_repo.mark_derived_unconfirmed(session, source.id, version.id)
                if touched:
                    report.count("facts_unconfirmed", touched)

        self._stage(
            report, version.id, source.id, JobStage.FINGERPRINT, lambda session: None
        )
        self._stage(report, version.id, source.id, JobStage.DIFF, lambda session: None)
        self._stage(
            report, version.id, source.id, JobStage.EXTRACT_METADATA, lambda session: None
        )

        if int(tier) < int(Tier.EMBED):
            return
        if not decision.needs_content_processing:
            # A duplicate keeps exactly one copy of the text and chunks (plan section L).
            if decision.change_type is ChangeType.DUPLICATE:
                report.count("duplicate_text_skipped")
                self._skip_content_stages(report, version.id, source.id, "duplicate content hash")
            return
        if not classification.policy.stores_text or classification.secret_suspected:
            self._skip_content_stages(
                report,
                version.id,
                source.id,
                "policy is metadata-only" if not classification.secret_suspected else "secret suspected",
            )
            return

        self._process_content(
            ctx, decision, source, version, fingerprint, classification, report, tier
        )

    # -------------------------------------------------------------------------------- sub-cases

    def _process_deleted(self, decision: ChangeDecision, report: RunReport) -> None:
        """Plan section L: ``status=deleted``; knowledge kept and flagged (ADR-0005 rule 4)."""
        assert decision.known is not None
        with self._scope.session() as session:
            sources = SourceRepo(session)
            sources.mark_deleted(decision.known.source_id)
            sources.record_event(
                SourceEvent(
                    id=new_id(),
                    source_id=decision.known.source_id,
                    version_id=decision.known.version_id,
                    event_type=SourceEventType.DELETED,
                    at=self._clock(),
                    run_id=report.run_id,
                    details={"uri": decision.uri, "reason": decision.reason},
                )
            )

    def _process_moved(
        self, ctx: RootContext, decision: ChangeDecision, report: RunReport
    ) -> None:
        """Same ``source_id``, new URI, event recorded, no re-extraction (plan section L)."""
        assert decision.known is not None and decision.observed is not None
        size_hint = decision.observed.size_bytes if decision.observed else 0
        classification = self._classify(
            ctx, decision.relative_path, None, size_hint=size_hint
        )
        with self._scope.session() as session:
            sources = SourceRepo(session)
            sources.mark_moved(decision.known.source_id, decision.uri, decision.known.uri)
            ingest_repo.apply_classification(
                session,
                decision.known.source_id,
                relative_path=decision.relative_path,
                policy=classification.policy.value,
                policy_reason=classification.reason,
                media_type=classification.media_type,
                origin=classification.origin.value,
                trust=classification.trust.value,
                secret_suspected=classification.secret_suspected,
                project_id=classification.project_id,
                at=self._clock(),
            )
            sources.record_event(
                SourceEvent(
                    id=new_id(),
                    source_id=decision.known.source_id,
                    version_id=decision.known.version_id,
                    event_type=SourceEventType.MOVED,
                    at=self._clock(),
                    run_id=report.run_id,
                    details={"from": decision.known.uri, "to": decision.uri},
                )
            )

    def _process_content(
        self,
        ctx: RootContext,
        decision: ChangeDecision,
        source: Source,
        version: SourceVersion,
        fingerprint: Fingerprint,
        classification: _Classification,
        report: RunReport,
        tier: Tier,
    ) -> None:
        """``extract_text -> chunk -> embed -> episode`` for one new/modified version.

        Every stage is resumable on its own: when ``extract_text`` is ``ALREADY_DONE`` the stored
        text is read back from ``source_text`` so the remaining stages can still run, which is what
        makes "killed mid-run, restarted" finish the job instead of leaving a half-processed version.
        """
        extracted: ExtractedText | None = None

        def _extract(session: Session) -> None:
            nonlocal extracted
            data = fingerprint.data
            if data is None:
                raise _StageSkipped("content not read (file above INGEST_MAX_TEXT_BYTES)")
            extractor = get_extractor(decision.relative_path, classification.media_type)
            if extractor is None:
                raise _StageSkipped("no extractor claims this file")
            result = extractor.extract(
                data,
                relative_path=decision.relative_path,
                max_bytes=self._settings.ingest.max_text_bytes,
            )
            if not result.ok:
                # Plan section P6-T02: a broken pdf/docx downgrades to CATALOG_ONLY with a recorded
                # reason; it does not fail the run.
                ingest_repo.apply_classification(
                    session,
                    source.id,
                    relative_path=decision.relative_path,
                    policy=StoragePolicy.CATALOG_ONLY.value,
                    policy_reason=f"extractor: {result.reason}",
                    media_type=classification.media_type,
                    origin=classification.origin.value,
                    trust=classification.trust.value,
                    secret_suspected=classification.secret_suspected,
                    project_id=classification.project_id,
                    at=self._clock(),
                )
                raise _StageSkipped(f"extractor: {result.reason}")
            SourceRepo(session).upsert_text(
                SourceText(
                    version_id=version.id,
                    text=result.text,
                    extractor=result.extractor,
                    extractor_version=result.extractor_version,
                    char_count=result.char_count,
                    truncated=result.truncated,
                    frontmatter=_json_safe(result.frontmatter),
                    links=result.links,
                    created_at=self._clock(),
                )
            )
            extracted = result
            report.count("texts_stored")

        outcome = self._stage(report, version.id, source.id, JobStage.EXTRACT_TEXT, _extract)
        if not outcome.ok:
            self._skip_content_stages(
                report,
                version.id,
                source.id,
                "no stored text",
                skip=(JobStage.CHUNK, JobStage.EMBED, JobStage.EPISODE),
            )
            return
        if extracted is None:
            # Resume path: the text is already in the database from an earlier run.
            with self._scope.session() as session:
                stored = ingest_repo.get_source_text(session, version.id)
            if stored is None:
                return
            extracted = stored
            report.count("resumed_versions")

        chunks: list[Chunk] = []

        def _chunk(session: Session) -> None:
            assert extracted is not None
            chunker = get_chunker(extracted.extractor)
            drafts = chunker.chunk(
                extracted.text,
                relative_path=decision.relative_path,
                max_tokens=CHUNK_MAX_TOKENS,
            )
            built = [
                chunk_from_draft(
                    draft,
                    # Deterministic id: replaying this stage rewrites the same rows (common/ids.py).
                    chunk_id=deterministic_id("chunk", version.id, draft.ordinal),
                    version_id=version.id,
                    source_id=source.id,
                    project_id=classification.project_id,
                )
                for draft in drafts
            ]
            ChunkRepo(session).bulk_insert(built)
            chunks.extend(built)
            report.count("chunks_written", len(built))

        self._stage(report, version.id, source.id, JobStage.CHUNK, _chunk)

        def _embed(session: Session) -> None:
            local_chunks = chunks or ChunkRepo(session).get_by_version(version.id)
            if not local_chunks:
                raise _StageSkipped("no chunks")
            embedder = self._embedding_provider()
            if embedder is None:
                raise _StageSkipped("no embedding provider configured")
            # embeddings.model_id is a foreign key into embedding_models, whose seeded id is a slug
            # ("minilm-l6-v2-384"), not the service's model *name*. Resolve (and register) it once.
            model_id = self._embedding_model_id(session, embedder)
            # Plan section L: embeddings are reused by text_hash, so a one-line edit re-embeds one
            # chunk, not the whole document.
            existing = ingest_repo.existing_embedding_hashes(
                session, [c.text_hash for c in local_chunks], model_id
            )
            todo: list[Chunk] = []
            seen: set[str] = set()
            for chunk in local_chunks:
                if chunk.text_hash in existing or chunk.text_hash in seen:
                    continue
                seen.add(chunk.text_hash)
                todo.append(chunk)
            report.count("embeddings_reused", len(local_chunks) - len(todo))
            if not todo:
                return
            result = embedder.embed([c.text for c in todo])
            rows = [
                Embedding(
                    id=deterministic_id("embedding", chunk.text_hash, model_id),
                    object_type=ObjectType.CHUNK,
                    object_id=chunk.id,
                    text_hash=chunk.text_hash,
                    model_id=model_id,
                    dimension=result.dimension,
                    vector=list(vector),
                    created_at=self._clock(),
                )
                for chunk, vector in zip(todo, result.vectors, strict=True)
            ]
            inserted = EmbeddingRepo(session).bulk_insert(rows)
            report.count("embeddings_written", inserted)

        self._stage(report, version.id, source.id, JobStage.EMBED, _embed)

        def _episode(session: Session) -> None:
            assert extracted is not None
            if classification.secret_suspected:
                # Never queue a suspected secret for extraction: under ADR-0012 the selected provider
                # may be AWS Bedrock, so this gate is a privacy control (asserted in tests/memory).
                raise _StageSkipped("secret suspected - never queued for extraction")
            status = (
                EpisodeStatus.QUEUED if int(tier) >= int(Tier.KNOWLEDGE) else EpisodeStatus.PENDING
            )
            repo = EpisodeRepo(session)
            repo.create(
                Episode(
                    id=new_id(),
                    type=EpisodeType.DOCUMENT,
                    source_id=source.id,
                    version_id=version.id,
                    project_id=classification.project_id,
                    title=decision.relative_path.rsplit("/", 1)[-1],
                    section_path=[],
                    body=extracted.text,
                    occurred_at=None,
                    observed_at=self._clock(),
                    status=status,
                    origin=classification.origin,
                    tier=Tier.KNOWLEDGE,
                    priority=classification.priority,
                    ingestion_run_id=report.run_id,
                )
            )
            report.count("episodes_queued")
            if decision.change_type is ChangeType.MODIFIED:
                repo.create(
                    Episode(
                        id=new_id(),
                        type=EpisodeType.DOCUMENT_CHANGE,
                        source_id=source.id,
                        version_id=version.id,
                        project_id=classification.project_id,
                        title=f"change: {decision.relative_path.rsplit('/', 1)[-1]}",
                        # A distinct section_path so this row can coexist with the document episode
                        # under the unique (version_id, section_path) index.
                        section_path=["__change__"],
                        body=extracted.text,
                        observed_at=self._clock(),
                        status=status,
                        origin=classification.origin,
                        tier=Tier.KNOWLEDGE,
                        priority=max(1, classification.priority - 1),
                        ingestion_run_id=report.run_id,
                    )
                )
                report.count("change_episodes")

        self._stage(report, version.id, source.id, JobStage.EPISODE, _episode)

    # ------------------------------------------------------------------------------ job plumbing

    def _stage(
        self,
        report: RunReport,
        version_id: UUID,
        source_id: UUID,
        stage: JobStage,
        body: Callable[[Session], None],
    ) -> StageOutcome:
        """Run one stage exactly once per ``(version_id, stage)`` - the idempotency key A04's schema
        enforces with a unique index.

        A failure is recorded on the job row with a sanitized message and the scan continues; the
        text, chunks and vectors written by earlier stages are left intact, and
        ``aimemory-ingest reprocess --failed`` can pick the stage up later (plan section L).
        """
        if report.run_id is None:  # dry run: nothing is written, so nothing is tracked
            return StageOutcome.DONE
        with self._scope.session() as session:
            if ingest_repo.already_done(session, version_id, stage):
                report.count(f"stage_{stage.value}_already_done")
                return StageOutcome.ALREADY_DONE
            JobRepo(session).upsert(
                IngestionJob(
                    id=new_id(),
                    run_id=report.run_id,
                    version_id=version_id,
                    source_id=source_id,
                    stage=stage,
                    state=JobState.RUNNING,
                    started_at=self._clock(),
                )
            )
        started = time.perf_counter()
        try:
            with self._scope.session() as session:
                body(session)
        except _StageSkipped as skipped:
            self._finish_job(
                report, version_id, source_id, stage, JobState.SKIPPED, started, skipped.reason
            )
            report.count(f"stage_{stage.value}_skipped")
            return StageOutcome.SKIPPED
        except Exception as exc:  # noqa: BLE001 - recorded, not raised: the run continues
            message = f"{type(exc).__name__}: {exc}"
            self._finish_job(report, version_id, source_id, stage, JobState.FAILED, started, message)
            report.count(f"stage_{stage.value}_failed")
            report.errors.append(f"{stage.value}: {message}")
            logger.warning("ingestion.stage_failed", stage=stage.value, error=message[:200])
            return StageOutcome.FAILED
        self._finish_job(report, version_id, source_id, stage, JobState.DONE, started, None)
        return StageOutcome.DONE

    def _finish_job(
        self,
        report: RunReport,
        version_id: UUID,
        source_id: UUID,
        stage: JobStage,
        state: JobState,
        started: float,
        error: str | None,
    ) -> None:
        assert report.run_id is not None
        with self._scope.session() as session:
            JobRepo(session).upsert(
                IngestionJob(
                    id=new_id(),
                    run_id=report.run_id,
                    version_id=version_id,
                    source_id=source_id,
                    stage=stage,
                    state=state,
                    finished_at=self._clock(),
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    error=error[:500] if error else None,
                )
            )

    def _skip_content_stages(
        self,
        report: RunReport,
        version_id: UUID,
        source_id: UUID,
        reason: str,
        skip: Sequence[JobStage] = (
            JobStage.EXTRACT_TEXT,
            JobStage.CHUNK,
            JobStage.EMBED,
            JobStage.EPISODE,
        ),
    ) -> None:
        if report.run_id is None:
            return
        with self._scope.session() as session:
            repo = JobRepo(session)
            for stage in skip:
                if ingest_repo.already_done(session, version_id, stage):
                    continue
                repo.upsert(
                    IngestionJob(
                        id=new_id(),
                        run_id=report.run_id,
                        version_id=version_id,
                        source_id=source_id,
                        stage=stage,
                        state=JobState.SKIPPED,
                        finished_at=self._clock(),
                        error=reason[:500],
                    )
                )

    # ---------------------------------------------------------------------------- classification

    def _embedding_provider(self) -> EmbeddingProvider | None:
        if self._embedder is None:
            from ..providers.embedding import HttpEmbeddingProvider

            try:
                self._embedder = HttpEmbeddingProvider(self._settings.embedding)
            except Exception:  # noqa: BLE001 - unreachable service is a skip, not a crash
                return None
        return self._embedder

    def _embedding_model_id(self, session: Session, embedder: EmbeddingProvider) -> str:
        """``embedding_models.id`` for the live provider, registering the row if it is unknown."""
        if self._embedding_model_key is None:
            self._embedding_model_key = ingest_repo.ensure_embedding_model(
                session, embedder.model_identity()
            )
        return self._embedding_model_key

    def _classify(
        self,
        ctx: RootContext,
        relative_path: str,
        fingerprint: Fingerprint | None,
        *,
        size_hint: int = 0,
    ) -> _Classification:
        """Policy + secret scan + origin/trust + project + Tier 2 priority for one path.

        The secret detector runs twice on purpose: first on the *filename* alone (``.env``, ``*.pem``,
        ``id_rsa*`` need no content), and then - only if the first pass would have stored text - on the
        bytes as well. That ordering means content is never scanned for a file that was going to be
        IGNORE/CATALOG_ONLY anyway, and a match can only ever downgrade the policy (plan section K).
        """
        size_bytes = fingerprint.size_bytes if fingerprint else size_hint
        secret = secret_scanner.scan(relative_path, None, ctx.secret_config)
        first = resolve_policy(
            relative_path,
            size_bytes=size_bytes,
            ignore_rules=ctx.ignore_rules,
            config=ctx.policies,
            root_override=ctx.override,
            secret_result=secret,
        )
        decision = first
        if first.policy.stores_text and fingerprint is not None and fingerprint.data is not None:
            secret = secret_scanner.scan(relative_path, fingerprint.data, ctx.secret_config)
            decision = resolve_policy(
                relative_path,
                size_bytes=size_bytes,
                ignore_rules=ctx.ignore_rules,
                config=ctx.policies,
                root_override=ctx.override,
                secret_result=secret,
            )
        origin, trust = ctx.origin_trust(relative_path)
        return _Classification(
            policy=decision.policy,
            reason=decision.reason,
            rule=decision.rule,
            secret_suspected=decision.secret_suspected or secret.suspected,
            secret_rules=tuple(secret.matched_rules),
            media_type=_media_type_for(relative_path),
            origin=origin,
            trust=trust,
            project_id=self._project_for(ctx, relative_path),
            priority=ctx.priority_for(relative_path),
        )

    def _project_for(self, ctx: RootContext, relative_path: str) -> str | None:
        """Attach the source to a project: an alias match on a path segment, else the root default.

        Only ids that exist in ``projects`` are returned - ``sources.project_id`` is a foreign key and
        the registry (Tier 0) may not have seeded a matching row.
        """
        for segment in reversed(relative_path.split("/")[:-1]):
            candidate = self._alias_map.get(normalize_name(segment))
            if candidate:
                return candidate
        default = ctx.root.default_project_id
        if default and default in self._project_ids:
            return default
        return None


@dataclass(frozen=True)
class _Classification:
    policy: StoragePolicy
    reason: str
    rule: str
    secret_suspected: bool
    secret_rules: tuple[str, ...]
    media_type: str | None
    origin: Origin
    trust: Trust
    project_id: str | None
    priority: int


_EVENT_FOR_CHANGE = {
    ChangeType.NEW: SourceEventType.CREATED,
    ChangeType.MODIFIED: SourceEventType.MODIFIED,
    ChangeType.DUPLICATE: SourceEventType.CREATED,
    ChangeType.MOVED: SourceEventType.MOVED,
    ChangeType.DELETED: SourceEventType.DELETED,
    ChangeType.UNCHANGED: SourceEventType.RESCANNED,
}

_MEDIA_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".py": "text/x-python",
    ".ipynb": "application/x-ipynb+json",
    ".sql": "application/sql",
    ".csv": "text/csv",
}


def _media_type_for(relative_path: str) -> str | None:
    name = relative_path.rsplit("/", 1)[-1].lower()
    index = name.rfind(".")
    return _MEDIA_TYPES.get(name[index:]) if index != -1 else None


def _event_details(decision: ChangeDecision, classification: _Classification) -> dict[str, str]:
    details = {"change": decision.change_type.value, "policy": classification.policy.value}
    if decision.counterpart_uri:
        details["counterpart"] = decision.counterpart_uri
    if decision.reason:
        details["reason"] = decision.reason
    return details


def _json_safe(value: Any) -> dict[str, Any]:
    """Frontmatter can contain dates/sets that ``jsonb`` cannot take; stringify anything exotic."""
    def _convert(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(k): _convert(v) for k, v in item.items()}
        if isinstance(item, (list, tuple, set)):
            return [_convert(v) for v in item]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        return str(item)

    converted = _convert(value)
    return converted if isinstance(converted, dict) else {}


def _batched(items: Sequence[UUID], size: int) -> Iterator[list[UUID]]:
    for index in range(0, len(items), size):
        yield list(items[index : index + size])
