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
    "abandon_stale_runs",
    "already_done",
    "apply_classification",
    "attach_root_project",
    "claim_episode_for_extraction",
    "count_chunks",
    "delete_run_request",
    "ensure_extraction_model",
    "episode_scope_ids",
    "episodes_for_version",
    "existing_embedding_hashes",
    "extraction_models_in_use",
    "facts_by_extraction_model",
    "fail_run_request",
    "failed_episode_ids",
    "finish_run_request",
    "equivalent_model_ids",
    "flag_other_model_facts",
    "get_job",
    "get_source_text",
    "known_project_ids",
    "known_sources_for_root",
    "mark_derived_unconfirmed",
    "project_alias_map",
    "queue_failed_episodes",
    "record_run_extraction_model",
    "requeue_episode",
    "requeue_episodes_for_reextraction",
    "requeue_failed_jobs",
    "source_events",
    "source_text_version_for_hash",
    "stage_state_counts",
    "status_report",
    "sync_source_roots",
    "tier1_backfill_needed",
    "touch_last_seen",
    "unembeddable_sources",
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


#: The four Tier 1 stages. A version is "settled" for Tier 1 once each of them has reached a
#: terminal state - ``done`` or ``skipped`` (CATALOG_ONLY, secret-suspected and no-extractor files
#: settle as ``skipped``, which is a correct outcome and must not be retried on every scan).
_TIER1_STAGES = ("extract_text", "chunk", "embed", "episode")


def tier1_backfill_needed(session: Session, version_id: UUID) -> bool:
    """True when Tier 1 work is still owed for this version, even though the file did not change.

    Two situations produce this, and both are normal:

    * the version was catalogued by a ``--tier 0`` run, so the content stages were never attempted
      (no ``ingestion_jobs`` rows exist for them at all) and a later ``--tier 1`` scan sees the file
      as ``unchanged``; and
    * a run was killed part-way through a version - some stages are ``done``, the rest are missing
      or were re-queued out of ``running`` by :meth:`JobRepo.requeue_stuck`.

    The embedding half is checked against the data rather than the job row as well, so a version
    whose ``embed`` stage was skipped because the embedding service was down (a ``skipped`` row, not
    a ``failed`` one) is picked up by the next scan instead of staying unembedded forever.
    """
    row = session.execute(
        text(
            """
            SELECT (SELECT count(*) FROM ingestion_jobs
                     WHERE version_id = :vid AND stage = ANY(:stages)
                       AND state IN ('done', 'skipped')),
                   (SELECT count(*) FROM chunks c
                     WHERE c.version_id = :vid
                       AND NOT EXISTS (SELECT 1 FROM embeddings e
                                        WHERE e.text_hash = c.text_hash))
            """
        ),
        {"vid": version_id, "stages": list(_TIER1_STAGES)},
    ).first()
    if row is None:  # pragma: no cover - the scalar subqueries always return a row
        return True
    settled, unembedded = int(row[0]), int(row[1])
    return settled < len(_TIER1_STAGES) or unembedded > 0


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


def claim_episode_for_extraction(
    session: Session, *, project_id: str | None = None, root_id: str | None = None
) -> dict[str, Any] | None:
    """Claim the next Tier 2 episode, honouring the ADR-0006 priority order.

    Mirrors :meth:`aimemory.persistence.repositories.EpisodeRepo.claim_next` but **excludes sources
    flagged ``secret_suspected``** as a second gate in the database itself: under ADR-0014 the default
    provider is AWS Bedrock, so episode text leaves the machine and "a suspected secret is never sent
    to an LLM" is a privacy control on egress, not hygiene. The pipeline also refuses to create such
    an episode at all - this query is the belt to that suspenders, and
    ``tests/memory/test_change_detection.py`` asserts both.

    ``project_id`` / ``root_id`` narrow the claim to one corpus scope, which is what
    ``reprocess --re-extract`` needs to re-run a single project under one model.
    """
    conditions = [
        "e.status IN ('pending', 'queued')",
        "coalesce(s.secret_suspected, false) = false",
    ]
    params: dict[str, Any] = {}
    if project_id:
        conditions.append("e.project_id = :pid")
        params["pid"] = project_id
    if root_id:
        conditions.append("s.root_id = :rid")
        params["rid"] = root_id
    row = session.execute(
        text(
            f"""
            UPDATE episodes SET status = 'running', updated_at = now()
             WHERE id = (
                SELECT e.id FROM episodes e
                  LEFT JOIN sources s ON s.id = e.source_id
                 WHERE {" AND ".join(conditions)}
                 ORDER BY e.priority ASC, e.created_at ASC
                 FOR UPDATE OF e SKIP LOCKED
                 LIMIT 1
             )
            RETURNING *
            """
        ),
        params,
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
                       -- "Embedded" is deliberately strict (P7-T02 acceptance is "100 % of
                       -- INDEX_CONTENT embedded"): the source must have chunks AND every one of
                       -- them must have a vector. Counting "has chunks" would report a source whose
                       -- embed stage failed halfway as fully covered.
                       count(*) FILTER (WHERE policy IN ('INDEX_CONTENT','MIRROR')
                                          AND status <> 'deleted'
                                          AND EXISTS (SELECT 1 FROM chunks c
                                                       WHERE c.source_id = sources.id)
                                          AND NOT EXISTS (
                                                SELECT 1 FROM chunks c
                                                 WHERE c.source_id = sources.id
                                                   AND NOT EXISTS (
                                                        SELECT 1 FROM embeddings e
                                                         WHERE e.text_hash = c.text_hash))
                                       ) AS embedded
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


def unembeddable_sources(
    session: Session, *, root_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """INDEX_CONTENT/MIRROR sources that produced no chunk, with the stage and reason.

    These are the entire difference between ``sources_embedded`` and ``sources_indexable``, so
    ``aimemory-ingest status`` prints them rather than leaving a coverage percentage below 100 %
    unexplained. Each one is a settled outcome, not a pending retry: a file whose extracted body is
    empty (a web clipping that is all YAML frontmatter, say) has nothing to embed until it changes.
    """
    rows = session.execute(
        text(
            """
            SELECT s.relative_path, s.root_id, j.stage, j.error
              FROM sources s
              LEFT JOIN ingestion_jobs j
                     ON j.version_id = s.current_version_id AND j.stage = 'embed'
             WHERE s.policy IN ('INDEX_CONTENT', 'MIRROR')
               AND s.status <> 'deleted'
               AND (cast(:rid AS text) IS NULL OR s.root_id = :rid)
               AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.source_id = s.id)
             ORDER BY s.relative_path
             LIMIT :n
            """
        ),
        {"rid": root_id, "n": limit},
    ).all()
    return [
        {"path": r[0], "root_id": r[1], "stage": r[2] or "embed", "reason": r[3] or "unknown"}
        for r in rows
    ]


def abandon_stale_runs(
    session: Session, *, root_id: str | None = None, exclude_run_id: UUID | None = None
) -> int:
    """Close ``ingestion_runs`` rows left ``running`` by a process that died (lease recovery).

    :meth:`JobRepo.requeue_stuck` already recovers the *job* rows, but nothing closed the run row
    itself, so every killed scan left a permanent ``running`` entry in ``status``'s "Recent runs"
    and in the Ops page. Scans of one root are serial by design (ADR-0011: the worker executes them
    one at a time), so any ``running`` row for this root that is not the run being started now
    belongs to a process that is gone.
    """
    result = session.execute(
        text(
            """
            UPDATE ingestion_runs
               SET status = 'failed',
                   finished_at = now(),
                   error = coalesce(error, 'abandoned: process exited before the run finished')
             WHERE status = 'running'
               AND (cast(:rid AS text) IS NULL OR root_id = :rid)
               AND (cast(:exclude AS uuid) IS NULL OR id <> :exclude)
            """
        ),
        {"rid": root_id, "exclude": exclude_run_id},
    )
    return int(result.rowcount or 0)


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


# --------------------------------------------------------------------------------------------------
# ADR-0014: one extraction model per corpus
# --------------------------------------------------------------------------------------------------


def ensure_extraction_model(session: Session, identity: Any) -> str:
    """Register an ``extraction_models`` row for a live provider identity. Returns its id.

    ``facts.extraction_model_id`` (and the same column on ``entity_mentions`` and
    ``knowledge_artifacts``) is a foreign key, so the model has to exist before anything stamped with
    it can be written. Migration 0001 seeds ``deterministic:registry-v1`` and ``qwen3-4b``; a Bedrock
    identity (``bedrock:us.anthropic.claude-haiku-4-5-...``) is registered on first use.
    """
    session.execute(
        text(
            """
            INSERT INTO extraction_models (id, provider, name, digest, parameters)
            VALUES (:id, :provider, :name, :digest, :parameters)
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": identity.id,
            "provider": getattr(identity, "provider", "unknown"),
            "name": identity.name,
            "digest": getattr(identity, "digest", None),
            "parameters": Jsonb({k: str(v) for k, v in (getattr(identity, "parameters", {}) or {}).items()}),
        },
    )
    return str(identity.id)


def extraction_models_in_use(
    session: Session, *, project_id: str | None = None, root_id: str | None = None
) -> dict[str, int]:
    """``{extraction_model_id: current fact count}`` for a corpus scope (ADR-0014 rule 2).

    Deterministic ids (``deterministic:*``) are excluded on purpose: Tier 0 seeds and the structural
    projection are produced without a model and are compatible with every model, so counting them
    would make the guard fire on a corpus no LLM has ever touched.
    """
    sql = [
        "SELECT extraction_model_id, count(*) FROM facts f",
        "WHERE f.status <> 'historical' AND f.valid_to IS NULL",
        "AND f.extraction_model_id IS NOT NULL",
        "AND f.extraction_model_id NOT LIKE 'deterministic:%'",
    ]
    params: dict[str, Any] = {}
    if project_id:
        sql.append("AND f.project_id = :pid")
        params["pid"] = project_id
    if root_id:
        sql.append("AND f.source_id IN (SELECT id FROM sources WHERE root_id = :rid)")
        params["rid"] = root_id
    sql.append("GROUP BY 1")
    rows = session.execute(text(" ".join(sql)), params).all()
    counts = {str(row[0]): int(row[1]) for row in rows}

    # Collapse ids that name the same model by a different route, or "more than one entry means the
    # corpus is mixed" would fire on a corpus extracted entirely by Claude Haiku 4.5 that happens to
    # have reached it down two paths. The reported id is the one with the most facts, so the label
    # stays truthful about where the bulk came from.
    for group in EQUIVALENT_MODEL_GROUPS:
        present = {mid: n for mid, n in counts.items() if mid in group}
        if len(present) > 1:
            primary = max(present, key=lambda mid: present[mid])
            for mid in present:
                if mid != primary:
                    counts.pop(mid)
            counts[primary] = sum(present.values())
    return counts


def facts_by_extraction_model(session: Session) -> dict[str, int]:
    """Whole-corpus view of the same question, for ``aimemory-ingest status``."""
    return extraction_models_in_use(session)


def episode_scope_ids(
    session: Session, *, project_id: str | None = None, root_id: str | None = None
) -> list[UUID]:
    """Episode ids inside a re-extraction scope (``reprocess --re-extract``)."""
    sql = ["SELECT e.id FROM episodes e WHERE true"]
    params: dict[str, Any] = {}
    if project_id:
        sql.append("AND e.project_id = :pid")
        params["pid"] = project_id
    if root_id:
        sql.append("AND e.source_id IN (SELECT id FROM sources WHERE root_id = :rid)")
        params["rid"] = root_id
    sql.append("ORDER BY e.priority ASC, e.created_at ASC")
    return [row[0] for row in session.execute(text(" ".join(sql)), params).all()]


def requeue_episodes_for_reextraction(
    session: Session, *, project_id: str | None = None, root_id: str | None = None
) -> int:
    """Put every already-extracted episode of a scope back on the Tier 2 queue.

    The old facts are **not** touched here: they are superseded by the re-extraction through the
    normal ADR-0005 path (A08 closes them as ``historical`` when the new generation contradicts
    them). ADR-0014 rule 2: *"the remedy is an explicit re-extraction, not a silent mix"*.
    """
    sql = [
        "UPDATE episodes SET status = 'queued', error = NULL, updated_at = now()",
        "WHERE status IN ('extracted', 'failed', 'skipped')",
    ]
    params: dict[str, Any] = {}
    if project_id:
        sql.append("AND project_id = :pid")
        params["pid"] = project_id
    if root_id:
        sql.append("AND source_id IN (SELECT id FROM sources WHERE root_id = :rid)")
        params["rid"] = root_id
    return int(session.execute(text(" ".join(sql)), params).rowcount)


#: Extraction model ids that name the *same underlying model* by a different route, and therefore do
#: not make a corpus "mixed" under ADR-0014 rule 2.
#:
#: That rule exists because ``qwen3:4b`` and Claude Haiku 4.5 disagree systematically about entity
#: types, so a corpus extracted by both is internally inconsistent. Two routes to Haiku 4.5 -
#: Bedrock and the ADR-0016 Claude Code relay - are the same weights answering the same prompt at
#: temperature 0. Flagging 1,245 facts ``unconfirmed`` to swap between them would impose the cost of
#: a real model change with none of the cause.
EQUIVALENT_MODEL_GROUPS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "claude-code:haiku-4-5",
        }
    ),
)


def equivalent_model_ids(model_id: str) -> list[str]:
    """``model_id`` plus any id naming the same model by another route. Always includes itself."""
    for group in EQUIVALENT_MODEL_GROUPS:
        if model_id in group:
            return sorted(group)
    return [model_id]


def flag_other_model_facts(
    session: Session, model_id: str, *, project_id: str | None = None, root_id: str | None = None
) -> int:
    """Mark a scope's current facts from *other* models ``unconfirmed`` before a re-extraction.

    Not a deletion and not a closure: ``valid_to`` stays ``NULL``, so the fact is still current and
    still answerable - it is simply no longer confirmed by the generation that is about to be
    written, and retrieval ranks it down (``boosts.unconfirmed_penalty``). Facts the new model
    re-observes are promoted back to ``current`` by A08's ``apply_fact``; facts it contradicts are
    closed as ``historical`` through the normal ADR-0005 path. Deterministic rows are left alone.
    """
    sql = [
        "UPDATE facts SET status = 'unconfirmed'",
        "WHERE status = 'current' AND valid_to IS NULL",
        "AND extraction_model_id IS NOT NULL",
        # Every id naming the same model, not just the one being written: see
        # EQUIVALENT_MODEL_GROUPS. Swapping route must not cost a re-confirmation of the corpus.
        "AND extraction_model_id <> ALL(CAST(:models AS text[]))",
        "AND extraction_model_id NOT LIKE 'deterministic:%'",
    ]
    params: dict[str, Any] = {"models": equivalent_model_ids(model_id)}
    if project_id:
        sql.append("AND project_id = :pid")
        params["pid"] = project_id
    if root_id:
        sql.append("AND source_id IN (SELECT id FROM sources WHERE root_id = :rid)")
        params["rid"] = root_id
    return int(session.execute(text(" ".join(sql)), params).rowcount)


def record_run_extraction_model(
    session: Session, run_id: UUID, *, model_id: str, allow_model_mix: bool
) -> None:
    """Record which extraction model a run used, and whether the mix guard was overridden.

    ADR-0014 rule 2 asks for this on ``ingestion_runs``; that table has no such column in A04's
    schema (0001/0002), and migrations are not A07a's to write - so the model id is recorded on the
    ``metrics_snapshots`` row for the run (``scope='ingestion_run'``, ``model_id`` is already a FK to
    ``extraction_models``) and the override is recorded as an integer counter on
    ``ingestion_runs.counters``, which is typed ``dict[str, int]``. See the NEEDS_HANDOFF note in the
    P6-T03/T04 result: A04 should add ``ingestion_runs.extraction_model_id`` and
    ``ingestion_runs.allow_model_mix`` so this stops being an indirection.
    """
    session.execute(
        text(
            "UPDATE ingestion_runs SET counters = counters || "
            "jsonb_build_object('allow_model_mix', :mix) WHERE id = :id"
        ),
        {"id": run_id, "mix": 1 if allow_model_mix else 0},
    )
    session.execute(
        text(
            """
            INSERT INTO metrics_snapshots (id, at, scope, run_id, model_id, extra)
            VALUES (:id, now(), 'ingestion_run', :run_id, :model_id, :extra)
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": _uuid5_for_run(run_id, model_id),
            "run_id": run_id,
            "model_id": model_id,
            "extra": Jsonb({"allow_model_mix": bool(allow_model_mix)}),
        },
    )


def _uuid5_for_run(run_id: UUID, model_id: str) -> UUID:
    from ..common.ids import deterministic_id

    return deterministic_id("metrics_snapshot", str(run_id), model_id)
