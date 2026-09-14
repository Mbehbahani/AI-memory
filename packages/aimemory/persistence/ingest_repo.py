"""Ingestion-side queries that the A04 repositories do not cover (A07a, P6-T03).

``packages/aimemory/persistence/repositories.py`` is A04's and is not edited here. This module adds
the reads and bulk updates the ingestion engine needs on top of it:

* **``source_roots`` synchronisation** - A04's P5 brief assigns the seeding of that table to A07a,
  because ``config/source-roots.yaml`` is git-ignored and is read by :mod:`aimemory.sources.roots`.
  It is re-synced at the start of every run, so editing the file and re-running is enough.
* **Change-detection reads** - the ``(uri, current content hash)`` picture of a root, in one query.
* **Idempotency reads** - "is this ``(version_id, stage)`` job already done", "which of these
  ``text_hash`` values already have an embedding", so a resumed run skips finished work instead of
  redoing it (plan section L, interruption row).
* **ADR-0005 rule 3** - facts/artifacts derived from a superseded source version become
  ``unconfirmed``; nothing is ever deleted.
* **``aimemory-ingest status``** - per-stage counts, coverage per project, failures (plan section AF).

Every function takes a live :class:`~sqlalchemy.orm.Session` and never opens a transaction, exactly
like A04's repositories.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..domain.enums import JobStage, JobState, SourceStatus
from ..domain.models import IngestionJob, SourceRoot

__all__ = [
    "CoverageRow",
    "KnownSourceRow",
    "StatusReport",
    "already_done",
    "apply_classification",
    "attach_root_project",
    "claim_episode_for_extraction",
    "count_chunks",
    "delete_run_request",
    "existing_embedding_hashes",
    "fail_run_request",
    "failed_episode_ids",
    "finish_run_request",
    "get_job",
    "known_project_ids",
    "known_sources_for_root",
    "mark_derived_unconfirmed",
    "project_alias_map",
    "queue_failed_episodes",
    "requeue_failed_jobs",
    "source_text_version_for_hash",
    "stage_state_counts",
    "status_report",
    "sync_source_roots",
    "touch_last_seen",
    "update_run_request_progress",
]


# --------------------------------------------------------------------------------------------------
# source_roots (A07a owns the seeding of this table)
# --------------------------------------------------------------------------------------------------


def sync_source_roots(session: Session, roots: Sequence[SourceRoot]) -> int:
    """Upsert every root from ``config/source-roots.yaml``. Returns the number of rows written.

    Disabled roots are written too (with ``enabled=false``) so the table documents what exists;
    ``default_project_id`` is only written when that project row already exists, because the column
    is a foreign key and the registry (Tier 0) may not have run yet.
    """
    written = 0
    for root in roots:
        project_exists = False
        if root.default_project_id:
            project_exists = bool(
                session.execute(
                    text("SELECT 1 FROM projects WHERE id = :pid"),
                    {"pid": root.default_project_id},
                ).first()
            )
        session.execute(
            text(
                """
                INSERT INTO source_roots (root_id, scheme, label, container_path, device_id,
                                           enabled, default_policy, default_project_id, kind,
                                           registry_role, priority_paths, mirror_paths,
                                           exclude_extra, origin_overrides)
                VALUES (:root_id, :scheme, :label, :container_path, :device_id, :enabled,
                        :default_policy, :default_project_id, :kind, :registry_role,
                        :priority_paths, :mirror_paths, :exclude_extra, :origin_overrides)
                ON CONFLICT (root_id) DO UPDATE SET
                    scheme = EXCLUDED.scheme, label = EXCLUDED.label,
                    container_path = EXCLUDED.container_path, device_id = EXCLUDED.device_id,
                    enabled = EXCLUDED.enabled, default_policy = EXCLUDED.default_policy,
                    default_project_id = coalesce(EXCLUDED.default_project_id,
                                                  source_roots.default_project_id),
                    kind = EXCLUDED.kind, registry_role = EXCLUDED.registry_role,
                    priority_paths = EXCLUDED.priority_paths,
                    mirror_paths = EXCLUDED.mirror_paths, exclude_extra = EXCLUDED.exclude_extra,
                    origin_overrides = EXCLUDED.origin_overrides
                """
            ),
            {
                "root_id": root.root_id,
                "scheme": root.scheme.value,
                "label": root.label,
                "container_path": str(root.container_path),
                "device_id": root.device_id,
                "enabled": root.enabled,
                "default_policy": root.default_policy.value,
                "default_project_id": root.default_project_id if project_exists else None,
                "kind": root.kind.value,
                "registry_role": root.registry_role,
                "priority_paths": list(root.priority_paths),
                "mirror_paths": list(root.mirror_paths),
                "exclude_extra": list(root.exclude_extra),
                "origin_overrides": Jsonb(list(root.origin_overrides)),
            },
        )
        written += 1
    return written


def attach_root_project(session: Session, root_id: str, project_id: str) -> None:
    """Set ``source_roots.default_project_id`` once the project row exists (Tier 0 ran)."""
    session.execute(
        text(
            "UPDATE source_roots SET default_project_id = :pid WHERE root_id = :rid "
            "AND EXISTS (SELECT 1 FROM projects WHERE id = :pid)"
        ),
        {"pid": project_id, "rid": root_id},
    )


# --------------------------------------------------------------------------------------------------
# Change detection
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class KnownSourceRow:
    source_id: UUID
    uri: str
    relative_path: str
    content_hash: str | None
    status: SourceStatus
    version_id: UUID | None


def known_sources_for_root(session: Session, root_id: str) -> list[KnownSourceRow]:
    """Every ``sources`` row of a root with the content hash of its current version."""
    rows = session.execute(
        text(
            """
            SELECT s.id, s.uri, s.relative_path, s.status, s.current_version_id, v.content_hash
              FROM sources s
              LEFT JOIN source_versions v ON v.id = s.current_version_id
             WHERE s.root_id = :root_id
            """
        ),
        {"root_id": root_id},
    ).all()
    return [
        KnownSourceRow(
            source_id=row[0],
            uri=row[1],
            relative_path=row[2],
            status=SourceStatus(row[3]),
            version_id=row[4],
            content_hash=row[5],
        )
        for row in rows
    ]


def touch_last_seen(session: Session, source_ids: Sequence[UUID], at: datetime) -> int:
    """The ``unchanged`` action of plan section L: refresh ``last_seen_at``, reprocess nothing."""
    if not source_ids:
        return 0
    return int(
        session.execute(
            text("UPDATE sources SET last_seen_at = :at WHERE id = ANY(:ids)"),
            {"at": at, "ids": list(source_ids)},
        ).rowcount
    )


def apply_classification(
    session: Session,
    source_id: UUID,
    *,
    relative_path: str,
    policy: str,
    policy_reason: str | None,
    media_type: str | None,
    origin: str,
    trust: str,
    secret_suspected: bool,
    project_id: str | None,
    at: datetime,
) -> None:
    """Re-apply the classification of an existing source (used after a move, or a policy change).

    ``SourceRepo.upsert_source`` keys on the URI and deliberately does not touch ``relative_path``;
    a renamed file keeps its ``source_id`` (plan section L) but everything derived from its *path* -
    policy, origin/trust, media type - has to be recomputed, which is what this does.
    """
    session.execute(
        text(
            """
            UPDATE sources
               SET relative_path = :rel, policy = :policy, policy_reason = :reason,
                   media_type = :media_type, origin = :origin, trust = :trust,
                   secret_suspected = :secret, project_id = coalesce(:project_id, project_id),
                   last_seen_at = :at
             WHERE id = :id
            """
        ),
        {
            "id": source_id,
            "rel": relative_path,
            "policy": policy,
            "reason": policy_reason,
            "media_type": media_type,
            "origin": origin,
            "trust": trust,
            "secret": secret_suspected,
            "project_id": project_id,
            "at": at,
        },
    )


def project_alias_map(session: Session) -> dict[str, str]:
    """``normalized_alias -> project_id``, used to attach a source to a project by folder name."""
    rows = session.execute(text("SELECT normalized_alias, project_id FROM project_aliases")).all()
    return {str(row[0]): str(row[1]) for row in rows}


def known_project_ids(session: Session) -> set[str]:
    return {str(row[0]) for row in session.execute(text("SELECT id FROM projects")).all()}


def source_text_version_for_hash(session: Session, content_hash: str) -> UUID | None:
    """The version id that already stores text for this content hash (duplicate handling).

    Plan section L: *"text/chunks once per hash"*. When a second URI shows up with content that is
    already stored, the pipeline links to this version instead of storing the text twice.
    """
    row = session.execute(
        text(
            """
            SELECT v.id
              FROM source_versions v
              JOIN source_text t ON t.version_id = v.id
             WHERE v.content_hash = :h
             ORDER BY v.created_at ASC
             LIMIT 1
            """
        ),
        {"h": content_hash},
    ).first()
    return row[0] if row else None


def mark_derived_unconfirmed(session: Session, source_id: UUID, keep_version_id: UUID) -> int:
    """ADR-0005 rule 3: facts/artifacts from an older version of this source become ``unconfirmed``.

    Nothing is deleted and ``valid_to`` stays ``NULL`` - the knowledge is still current, it simply was
    not re-observed in the new version, and retrieval ranks it down until it is.
    """
    facts = session.execute(
        text(
            """
            UPDATE facts SET status = 'unconfirmed'
             WHERE source_id = :sid
               AND status = 'current'
               AND valid_to IS NULL
               AND (source_version IS NULL OR source_version <> :vid)
            """
        ),
        {"sid": source_id, "vid": keep_version_id},
    ).rowcount
    artifacts = session.execute(
        text(
            """
            UPDATE knowledge_artifacts SET current_status = 'unconfirmed'
             WHERE source_id = :sid
               AND current_status = 'current'
               AND valid_to IS NULL
               AND (source_version IS NULL OR source_version <> :vid)
            """
        ),
        {"sid": source_id, "vid": keep_version_id},
    ).rowcount
    return int(facts) + int(artifacts)


# --------------------------------------------------------------------------------------------------
# Idempotency / resume
# --------------------------------------------------------------------------------------------------


def get_job(session: Session, version_id: UUID, stage: JobStage) -> IngestionJob | None:
    """The ``(version_id, stage)`` job row, if one exists (the DB enforces uniqueness)."""
    row = session.execute(
        text("SELECT * FROM ingestion_jobs WHERE version_id = :vid AND stage = :stage"),
        {"vid": version_id, "stage": stage.value},
    ).first()
    if row is None:
        return None
    return IngestionJob(**dict(row._mapping))


def already_done(session: Session, version_id: UUID, stage: JobStage) -> bool:
    """True when this stage finished for this version in an earlier (possibly killed) run."""
    row = session.execute(
        text(
            "SELECT 1 FROM ingestion_jobs WHERE version_id = :vid AND stage = :stage "
            "AND state = 'done'"
        ),
        {"vid": version_id, "stage": stage.value},
    ).first()
    return row is not None


def existing_embedding_hashes(
    session: Session, text_hashes: Iterable[str], model_id: str
) -> set[str]:
    """Which of these ``text_hash`` values already have a vector for ``model_id``.

    This is the embedding-reuse key of plan section L: a modified document re-chunks, but only the
    chunks whose text actually changed are sent to the embedding service.
    """
    hashes = list(dict.fromkeys(text_hashes))
    if not hashes:
        return set()
    rows = session.execute(
        text("SELECT text_hash FROM embeddings WHERE model_id = :m AND text_hash = ANY(:hashes)"),
        {"m": model_id, "hashes": hashes},
    ).all()
    return {row[0] for row in rows}


def get_source_text(session: Session, version_id: UUID) -> Any | None:
    """Stored text of a version, shaped as an :class:`~aimemory.domain.ports.ExtractedText`.

    Used by the resume path: when ``extract_text`` was already ``done`` in a killed run, the later
    stages still need the text, and re-reading the file would be both wasteful and wrong (the file
    may have changed again since).
    """
    from ..domain.ports import ExtractedText  # local import keeps persistence import-light

    row = session.execute(
        text(
            "SELECT text, extractor, extractor_version, char_count, truncated, frontmatter, links "
            "FROM source_text WHERE version_id = :vid"
        ),
        {"vid": version_id},
    ).first()
    if row is None:
        return None
    return ExtractedText(
        text=row[0],
        extractor=row[1],
        extractor_version=row[2],
        char_count=row[3],
        truncated=row[4],
        frontmatter=row[5] or {},
        links=list(row[6] or []),
        ok=True,
    )


def ensure_embedding_model(session: Session, identity: Any) -> str:
    """Return the ``embedding_models.id`` for a live provider identity, inserting it if unknown.

    ``embeddings.model_id`` is a foreign key. Migration 0001 seeds the MiniLM row with the slug
    ``minilm-l6-v2-384`` while the provider reports the *name*
    ``sentence-transformers/all-MiniLM-L6-v2`` and its own slug - so the lookup is by **name** first
    (which matches the seeded row) and only then falls back to registering a new row. That keeps the
    embedding-reuse key ``(text_hash, model_id)`` stable across processes instead of forking into two
    ids for the same model.
    """
    row = session.execute(
        text("SELECT id FROM embedding_models WHERE name = :name ORDER BY created_at LIMIT 1"),
        {"name": identity.name},
    ).first()
    if row is not None:
        return str(row[0])
    session.execute(
        text(
            """
            INSERT INTO embedding_models (id, name, dimension, revision, normalized, max_seq)
            VALUES (:id, :name, :dimension, :revision, :normalized, :max_seq)
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": identity.id,
            "name": identity.name,
            "dimension": identity.dimension,
            "revision": identity.revision,
            "normalized": identity.normalized,
            "max_seq": identity.max_seq,
        },
    )
    return str(identity.id)


def count_chunks(session: Session, version_id: UUID) -> int:
    row = session.execute(
        text("SELECT count(*) FROM chunks WHERE version_id = :vid"), {"vid": version_id}
    ).first()
    return int(row[0]) if row else 0


def requeue_failed_jobs(session: Session, *, root_id: str | None = None) -> int:
    """``reprocess --failed``: put failed jobs back to ``pending`` (their writes stay idempotent)."""
    sql = "UPDATE ingestion_jobs SET state = 'pending', error = NULL WHERE state = 'failed'"
    params: dict[str, Any] = {}
    if root_id:
        sql += " AND source_id IN (SELECT id FROM sources WHERE root_id = :rid)"
        params["rid"] = root_id
    return int(session.execute(text(sql), params).rowcount)


def queue_failed_episodes(session: Session, *, root_id: str | None = None) -> int:
    """``reprocess --failed``: re-queue failed Tier 2 episodes. Vectors are untouched."""
    sql = (
        "UPDATE episodes SET status = 'queued', error = NULL, updated_at = now() "
        "WHERE status = 'failed'"
    )
    params: dict[str, Any] = {}
    if root_id:
        sql += " AND source_id IN (SELECT id FROM sources WHERE root_id = :rid)"
        params["rid"] = root_id
    return int(session.execute(text(sql), params).rowcount)


def requeue_episode(session: Session, episode_id: UUID) -> None:
    """Put one claimed episode back on the queue (used when a run cannot finish it)."""
    session.execute(
        text(
            "UPDATE episodes SET status = 'queued', updated_at = now() WHERE id = :id "
            "AND status = 'running'"
        ),
        {"id": episode_id},
    )


def episodes_for_version(session: Session, version_id: UUID) -> list[dict[str, Any]]:
    """Every episode of one source version, newest first. ``EpisodeRepo`` (A04) has no such query and
    the scenario suite needs one; it lives here rather than in A04's file."""
    rows = session.execute(
        text("SELECT * FROM episodes WHERE version_id = :vid ORDER BY created_at DESC"),
        {"vid": version_id},
    ).all()
    return [dict(row._mapping) for row in rows]


def source_events(session: Session, source_id: UUID) -> list[dict[str, Any]]:
    """Append-only event log of one source (``created``/``modified``/``moved``/``deleted``/...)."""
    rows = session.execute(
        text("SELECT * FROM source_events WHERE source_id = :sid ORDER BY at ASC"),
        {"sid": source_id},
    ).all()
    return [dict(row._mapping) for row in rows]


def failed_episode_ids(session: Session, *, limit: int = 50) -> list[UUID]:
    rows = session.execute(
        text("SELECT id FROM episodes WHERE status = 'failed' ORDER BY updated_at DESC LIMIT :n"),
        {"n": limit},
    ).all()
    return [row[0] for row in rows]


def claim_episode_for_extraction(session: Session) -> dict[str, Any] | None:
    """Claim the next Tier 2 episode, honouring the ADR-0006 priority order.

    Mirrors :meth:`aimemory.persistence.repositories.EpisodeRepo.claim_next` but **excludes sources
    flagged ``secret_suspected``** as a second gate in the database itself: under ADR-0012 a Bedrock
    run transmits episode text off-machine, so "a suspected secret is never sent to an LLM" is a
    privacy control, not hygiene. The pipeline also refuses to create such an episode at all.
    """
    row = session.execute(
        text(
            """
            UPDATE episodes SET status = 'running', updated_at = now()
             WHERE id = (
                SELECT e.id FROM episodes e
                  LEFT JOIN sources s ON s.id = e.source_id
                 WHERE e.status IN ('pending', 'queued')
                   AND coalesce(s.secret_suspected, false) = false
                 ORDER BY e.priority ASC, e.created_at ASC
                 FOR UPDATE OF e SKIP LOCKED
                 LIMIT 1
             )
            RETURNING *
            """
        )
    ).first()
    return dict(row._mapping) if row is not None else None


# --------------------------------------------------------------------------------------------------
# run_requests (ADR-0011 worker)
# --------------------------------------------------------------------------------------------------


def update_run_request_progress(
    session: Session,
    request_id: UUID,
    *,
    progress_pct: int,
    message: str | None = None,
    run_id: UUID | None = None,
) -> None:
    session.execute(
        text(
            """
            UPDATE run_requests
               SET progress_pct = :pct,
                   message = coalesce(:msg, message),
                   run_id = coalesce(:run_id, run_id)
             WHERE id = :id
            """
        ),
        {
            "id": request_id,
            "pct": max(0, min(100, int(progress_pct))),
            "msg": message,
            "run_id": run_id,
        },
    )


def finish_run_request(
    session: Session, request_id: UUID, *, message: str | None = None, run_id: UUID | None = None
) -> None:
    session.execute(
        text(
            """
            UPDATE run_requests
               SET status = 'done', progress_pct = 100, finished_at = now(),
                   message = coalesce(:msg, message), run_id = coalesce(:run_id, run_id)
             WHERE id = :id
            """
        ),
        {"id": request_id, "msg": message, "run_id": run_id},
    )


def fail_run_request(session: Session, request_id: UUID, *, error: str) -> None:
    session.execute(
        text(
            """
            UPDATE run_requests
               SET status = 'failed', finished_at = now(), error = :error
             WHERE id = :id
            """
        ),
        {"id": request_id, "error": error[:2000]},
    )


def delete_run_request(session: Session, request_id: UUID) -> None:
    """Only used by tests to clean up after themselves."""
    session.execute(text("DELETE FROM run_requests WHERE id = :id"), {"id": request_id})


# --------------------------------------------------------------------------------------------------
# status (plan section AF metrics, ADR-0011 Ops page)
# --------------------------------------------------------------------------------------------------


@dataclass
class CoverageRow:
    """Tier 2 coverage for one project: extracted episodes / INDEX_CONTENT sources."""

    project_id: str
    sources_indexable: int
    sources_embedded: int
    episodes_total: int
    episodes_extracted: int
    episodes_failed: int

    @property
    def embed_coverage(self) -> float:
        return self.sources_embedded / self.sources_indexable if self.sources_indexable else 0.0

    @property
    def extraction_coverage(self) -> float:
        return self.episodes_extracted / self.episodes_total if self.episodes_total else 0.0


@dataclass
class StatusReport:
    """Everything ``aimemory-ingest status`` prints (and the Ops page will render)."""

    sources_by_status: dict[str, int] = field(default_factory=dict)
    sources_by_policy: dict[str, int] = field(default_factory=dict)
    sources_by_root: dict[str, int] = field(default_factory=dict)
    secret_suspected: int = 0
    versions: int = 0
    texts: int = 0
    chunks: int = 0
    embeddings: int = 0
    chunks_without_embedding: int = 0
    stages: dict[str, dict[str, int]] = field(default_factory=dict)
    episodes_by_status: dict[str, int] = field(default_factory=dict)
    coverage: list[CoverageRow] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    last_runs: list[dict[str, Any]] = field(default_factory=list)
    projects: int = 0
    entities: int = 0
    facts: int = 0
    artifacts: int = 0
    generated_at: datetime | None = None


def _counts(session: Session, sql: str, params: dict[str, Any] | None = None) -> dict[str, int]:
    rows = session.execute(text(sql), params or {}).all()
    return {str(row[0]): int(row[1]) for row in rows}


def status_report(session: Session, *, root_id: str | None = None, failures: int = 10) -> StatusReport:
    """Build the full status report in a handful of aggregate queries."""
    filter_root = " WHERE root_id = :rid" if root_id else ""
    params: dict[str, Any] = {"rid": root_id} if root_id else {}

    report = StatusReport()
    report.sources_by_status = _counts(
        session, f"SELECT status, count(*) FROM sources{filter_root} GROUP BY status", params
    )
    report.sources_by_policy = _counts(
        session, f"SELECT policy, count(*) FROM sources{filter_root} GROUP BY policy", params
    )
    report.sources_by_root = _counts(
        session, f"SELECT root_id, count(*) FROM sources{filter_root} GROUP BY root_id", params
    )
    row = session.execute(
        text(f"SELECT count(*) FROM sources{filter_root or ' WHERE true'} AND secret_suspected"),
        params,
    ).first()
    report.secret_suspected = int(row[0]) if row else 0

    scalar_sql = {
        "versions": "SELECT count(*) FROM source_versions",
        "texts": "SELECT count(*) FROM source_text",
        "chunks": "SELECT count(*) FROM chunks",
        "embeddings": "SELECT count(*) FROM embeddings",
    }
    for attr, sql in scalar_sql.items():
        value = session.execute(text(sql)).scalar() or 0
        setattr(report, attr, int(value))
    report.chunks_without_embedding = int(
        session.execute(
            text(
                "SELECT count(*) FROM chunks c "
                "WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.text_hash = c.text_hash)"
            )
        ).scalar()
        or 0
    )

    stage_rows = session.execute(
        text("SELECT stage, state, count(*) FROM ingestion_jobs GROUP BY stage, state")
    ).all()
    stages: dict[str, dict[str, int]] = {}
    for stage, state, count in stage_rows:
        stages.setdefault(str(stage), {})[str(state)] = int(count)
    report.stages = stages

    report.episodes_by_status = _counts(
        session, "SELECT status, count(*) FROM episodes GROUP BY status"
    )

    coverage_rows = session.execute(
        text(
            """
            WITH src AS (
                SELECT coalesce(project_id, '(unassigned)') AS project_id,
                       count(*) FILTER (WHERE policy IN ('INDEX_CONTENT','MIRROR')
                                          AND status <> 'deleted') AS indexable,
                       count(*) FILTER (WHERE policy IN ('INDEX_CONTENT','MIRROR')
                                          AND status <> 'deleted'
                                          AND EXISTS (SELECT 1 FROM chunks c
                                                       WHERE c.source_id = sources.id)) AS embedded
                  FROM sources
                 GROUP BY 1
            ), eps AS (
                SELECT coalesce(project_id, '(unassigned)') AS project_id,
                       count(*) AS total,
                       count(*) FILTER (WHERE status = 'extracted') AS extracted,
                       count(*) FILTER (WHERE status = 'failed') AS failed
                  FROM episodes
                 GROUP BY 1
            )
            SELECT coalesce(src.project_id, eps.project_id) AS project_id,
                   coalesce(src.indexable, 0), coalesce(src.embedded, 0),
                   coalesce(eps.total, 0), coalesce(eps.extracted, 0), coalesce(eps.failed, 0)
              FROM src FULL OUTER JOIN eps ON eps.project_id = src.project_id
             ORDER BY 1
            """
        )
    ).all()
    report.coverage = [
        CoverageRow(
            project_id=str(r[0]),
            sources_indexable=int(r[1]),
            sources_embedded=int(r[2]),
            episodes_total=int(r[3]),
            episodes_extracted=int(r[4]),
            episodes_failed=int(r[5]),
        )
        for r in coverage_rows
    ]

    failure_rows = session.execute(
        text(
            """
            SELECT j.stage, j.error, s.uri, j.finished_at
              FROM ingestion_jobs j
              LEFT JOIN sources s ON s.id = j.source_id
             WHERE j.state = 'failed'
             ORDER BY j.finished_at DESC NULLS LAST
             LIMIT :n
            """
        ),
        {"n": failures},
    ).all()
    report.failures = [
        {"stage": r[0], "error": r[1], "uri": r[2], "at": r[3]} for r in failure_rows
    ]

    run_rows = session.execute(
        text(
            """
            SELECT id, root_id, tier, status, started_at, finished_at, counters
              FROM ingestion_runs ORDER BY started_at DESC LIMIT 5
            """
        )
    ).all()
    report.last_runs = [
        {
            "id": r[0],
            "root_id": r[1],
            "tier": r[2],
            "status": r[3],
            "started_at": r[4],
            "finished_at": r[5],
            "counters": r[6],
        }
        for r in run_rows
    ]

    for attr, sql in {
        "projects": "SELECT count(*) FROM projects",
        "entities": "SELECT count(*) FROM entities",
        "facts": "SELECT count(*) FROM facts",
        "artifacts": "SELECT count(*) FROM knowledge_artifacts",
    }.items():
        setattr(report, attr, int(session.execute(text(sql)).scalar() or 0))

    report.generated_at = session.execute(text("SELECT now()")).scalar()
    return report


def recent_stage_durations_ms(session: Session, stage: JobStage, *, limit: int = 200) -> list[int]:
    """Latest per-stage durations - the "median seconds per episode" metric of plan section AF."""
    rows = session.execute(
        text(
            "SELECT duration_ms FROM ingestion_jobs WHERE stage = :s AND duration_ms IS NOT NULL "
            "ORDER BY finished_at DESC NULLS LAST LIMIT :n"
        ),
        {"s": stage.value, "n": limit},
    ).all()
    return [int(row[0]) for row in rows]


def stage_state_counts(session: Session, stage: JobStage) -> dict[JobState, int]:
    rows = session.execute(
        text("SELECT state, count(*) FROM ingestion_jobs WHERE stage = :s GROUP BY state"),
        {"s": stage.value},
    ).all()
    return {JobState(row[0]): int(row[1]) for row in rows}
