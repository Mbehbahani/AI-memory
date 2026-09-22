"""Read-only SQL behind every `/ops` section (ADR-0010/ADR-0011, plan section AF/AG).

Every function takes a live :class:`~sqlalchemy.orm.Session` (the caller controls the transaction,
same convention as :mod:`aimemory.persistence.repositories`) and returns a
:mod:`aimemory.ops.viewmodels` dataclass - never a raw row. Nothing here writes; the two writes the
page performs (enqueue a run, record a review verdict) live in :mod:`aimemory.ops.actions` and go
through :class:`aimemory.persistence.repositories.MetricsRepo`, the same path the worker and the CLI
use, so a request created by the page is byte-for-byte a request the worker already knows how to run.

Coordinates with A04's tables (``run_requests``, ``metrics_snapshots``, ``extraction_reviews``,
``service_stats``) and reuses A04's :func:`aimemory.persistence.ingest_repo.status_report` for the
coverage numbers rather than re-deriving them, so the Ops page and ``aimemory-ingest status`` can never
silently disagree.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.time import utc_now
from ..sources.freshness import SERVICE as FRESHNESS_SERVICE
from ..domain.provenance import Provenance
from ..gateway import ComponentHealth, Gateway
from ..persistence import ingest_repo
from .viewmodels import (
    NotesView,
    NoteRow,
    AttentionItem,
    AttentionView,
    BackupStatus,
    ComponentCheck,
    CoverageProjectRow,
    CoverageView,
    DiskStatus,
    FreshnessView,
    HealthView,
    ModelUsageRow,
    PurposeLatencyRow,
    QualityView,
    RecentCallRow,
    ReviewCandidate,
    ReviewQueueView,
    RunRequestRow,
    RunsView,
    SnapshotPoint,
    SourceRootOption,
    UsageView,
    WorkerStatus,
)

__all__ = [
    "attention_view",
    "backup_status",
    "coverage_view",
    "disk_status",
    "health_view",
    "quality_view",
    "review_queue_view",
    "runs_view",
    "usage_view",
    "worker_status",
]

#: extraction is non-streaming structured output (ADR-0009): the whole completion arrives at once,
#: so there is no first token to time separately from the last. Told on the page, not hidden.
_TTFT_NOTE = (
    "Time-to-first-token is not measurable for this workload: extraction calls the model for "
    "non-streaming structured output, so the response only exists once it is complete - there is no "
    "first token to time separately from the last. Total latency (duration, below) and output "
    "throughput (completion tokens/second) are the meaningful equivalents here."
)

#: A worker that has not written a ``service_stats`` row in this long is presumed not running
#: (compose default ``INGEST_WORKER_POLL_SECONDS=5``; five minutes is generous slack).
_WORKER_STALE_SECONDS = 300

REPORTS_DIR = Path(__file__).resolve().parents[3] / "reports"
BACKUPS_DIR = Path(__file__).resolve().parents[3] / "backups" / "postgres"


# ====================================================================================================
# Health (plan section AG)
# ====================================================================================================


def worker_status(session: Session) -> WorkerStatus:
    row = session.execute(
        text(
            "SELECT at, model_loaded, model_name, process_rss_bytes, details "
            "FROM service_stats WHERE service = 'ingestion' ORDER BY at DESC LIMIT 1"
        )
    ).first()
    if row is None:
        return WorkerStatus(seen=False)
    at, model_loaded, model_name, rss, details = row
    age = (utc_now() - at).total_seconds() if at is not None else None
    return WorkerStatus(
        seen=True,
        at=at,
        model_loaded=model_loaded,
        model_name=model_name,
        process_rss_bytes=rss,
        llm_provider=(details or {}).get("llm_provider"),
        stale=bool(age is not None and age > _WORKER_STALE_SECONDS),
        stale_after_seconds=_WORKER_STALE_SECONDS,
    )


def backup_status(*, backups_dir: Path | None = None) -> BackupStatus:
    """Newest file under ``backups/postgres`` - the whole directory is a **host** path.

    memory-api's container has no bind mount onto ``D:\\AI memory\\backups`` in the shipped compose
    file (ADR-0011 keeps the Docker socket, and every host path, out of every container), so from
    inside the running service this almost always resolves to "not mounted" - reported as such,
    honestly, rather than guessed at. If a future compose change adds a read-only mount at
    ``/app/backups``, this starts reporting real ages with no code change.
    """
    directory = backups_dir or BACKUPS_DIR
    if not directory.is_dir():
        return BackupStatus(
            mounted=False,
            note=(
                "backups/postgres is not visible to this container (no bind mount in "
                "docker-compose.yml); run scripts/backup.ps1 and check the host directory directly, "
                "or mount it read-only to get this number on the page"
            ),
        )
    newest: tuple[str, float] | None = None
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        mtime = entry.stat().st_mtime
        if newest is None or mtime > newest[1]:
            newest = (entry.name, mtime)
    if newest is None:
        return BackupStatus(mounted=True, note="backups/postgres is mounted but empty - no backup taken yet")
    name, mtime = newest
    newest_at = datetime.fromtimestamp(mtime).astimezone()
    age_seconds = (utc_now() - newest_at).total_seconds()
    return BackupStatus(mounted=True, newest_at=newest_at, newest_name=name, age_seconds=age_seconds)


def disk_status(*, path: str = "/") -> DiskStatus:
    """The container's own filesystem - not the Windows host (labelled on the template)."""
    usage = shutil.disk_usage(path)
    return DiskStatus(total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free, path=path)


def health_view(session: Session, gateway: Gateway) -> HealthView:
    report = gateway.health()
    checks = [
        ComponentCheck(name=c.name, ok=c.ok, detail=c.detail, latency_ms=c.latency_ms)
        for c in report.checks
    ]
    return HealthView(
        status=report.status,
        writes_enabled=report.writes_enabled,
        checks=checks,
        worker=worker_status(session),
        backup=backup_status(),
        disk=disk_status(),
        generated_at=utc_now(),
    )


# ====================================================================================================
# Runs (plan section AG: Run scan / Retry failed / Run eval / Run benchmark)
# ====================================================================================================


def source_root_options(session: Session) -> list[SourceRootOption]:
    rows = session.execute(
        text("SELECT root_id, label FROM source_roots WHERE enabled ORDER BY root_id")
    ).all()
    return [SourceRootOption(root_id=r[0], label=r[1]) for r in rows]


def runs_view(session: Session, *, limit: int = 20) -> RunsView:
    rows = session.execute(
        text(
            """
            SELECT id, action, root_id, tier, status, progress_pct, message, requested_by,
                   requested_at, started_at, finished_at, error
              FROM run_requests
             ORDER BY requested_at DESC
             LIMIT :n
            """
        ),
        {"n": limit},
    ).all()
    recent = [
        RunRequestRow(
            id=r[0],
            action=r[1],
            root_id=r[2],
            tier=r[3],
            status=r[4],
            progress_pct=r[5],
            message=r[6],
            requested_by=r[7],
            requested_at=r[8],
            started_at=r[9],
            finished_at=r[10],
            error=r[11],
        )
        for r in rows
    ]
    pending_episodes = int(
        session.execute(text("SELECT count(*) FROM episodes WHERE status = 'pending'")).scalar() or 0
    )
    queued_or_running = int(
        session.execute(
            text("SELECT count(*) FROM run_requests WHERE status IN ('queued', 'running')")
        ).scalar()
        or 0
    )
    return RunsView(
        recent=recent,
        roots=source_root_options(session),
        pending_episodes=pending_episodes,
        queued_or_running=queued_or_running,
        freshness=freshness_view(session),
    )


def freshness_view(session: Session) -> FreshnessView:
    """The newest freshness check the ingestion worker recorded.

    A read, and only a read. The comparison itself needs the source folders, which `memory-api`
    cannot see - it is the one service exposed over HTTP, and mounting the vault into it to power a
    status widget would be a poor trade. The worker does the work and leaves the answer in
    `service_stats`; this reads the latest row.

    Missing row -> `has_check=False`, rendered as "not checked yet". Never zero: "0 files changed"
    from a check that never ran is the most dangerous sentence this page could print.
    """
    row = session.execute(
        text(
            """
            SELECT at, details
              FROM service_stats
             WHERE service = :service
             ORDER BY at DESC
             LIMIT 1
            """
        ),
        {"service": FRESHNESS_SERVICE},
    ).first()
    if row is None:
        return FreshnessView()
    details = row[1] if isinstance(row[1], dict) else {}

    def _int(key: str) -> int:
        try:
            return int(details.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    def _list(key: str) -> list[str]:
        value = details.get(key)
        return [str(v) for v in value] if isinstance(value, list) else []

    return FreshnessView(
        checked_at=row[0],
        files_on_disk=_int("files_on_disk"),
        unchanged=_int("unchanged"),
        changed=_int("changed"),
        added=_int("added"),
        removed=_int("removed"),
        touched_not_changed=_int("touched_not_changed"),
        hashed=_int("hashed"),
        duration_ms=_int("duration_ms"),
        roots_checked=_list("roots_checked"),
        changed_sample=_list("changed_sample"),
        added_sample=_list("added_sample"),
        removed_sample=_list("removed_sample"),
        errors=_list("errors"),
        has_check=True,
    )


# ====================================================================================================
# Coverage (per project and tier; queue length + ETA from measured seconds/episode)
# ====================================================================================================


def _latest_median_seconds_per_episode(session: Session) -> tuple[float | None, str]:
    """Prefer the last written ``metrics_snapshots`` row (what the worker itself measured after a
    run); fall back to the raw ``ingestion_jobs`` durations so the page still has a number before the
    first snapshot exists."""
    row = session.execute(
        text(
            "SELECT median_seconds_per_episode, at FROM metrics_snapshots "
            "WHERE scope = 'ingestion_run' AND median_seconds_per_episode IS NOT NULL "
            "ORDER BY at DESC LIMIT 1"
        )
    ).first()
    if row is not None and row[0] is not None:
        return float(row[0]), "metrics_snapshots (latest ingestion_run snapshot)"
    from ..domain.enums import JobStage

    durations = ingest_repo.recent_stage_durations_ms(session, JobStage.EXTRACT_KNOWLEDGE)
    if durations:
        import statistics

        return statistics.median(durations) / 1000.0, "ingestion_jobs (recent extract_knowledge durations)"
    return None, "no timing measured yet"


def coverage_view(session: Session) -> CoverageView:
    report = ingest_repo.status_report(session)
    median_seconds, source = _latest_median_seconds_per_episode(session)
    rows = [
        CoverageProjectRow(
            project_id=row.project_id,
            sources_indexable=row.sources_indexable,
            sources_embedded=row.sources_embedded,
            episodes_total=row.episodes_total,
            episodes_extracted=row.episodes_extracted,
            episodes_failed=row.episodes_failed,
        )
        for row in report.coverage
    ]
    totals = CoverageProjectRow(
        project_id="(all projects)",
        sources_indexable=sum(r.sources_indexable for r in rows),
        sources_embedded=sum(r.sources_embedded for r in rows),
        episodes_total=sum(r.episodes_total for r in rows),
        episodes_extracted=sum(r.episodes_extracted for r in rows),
        episodes_failed=sum(r.episodes_failed for r in rows),
    )
    return CoverageView(
        rows=rows, median_seconds_per_episode=median_seconds, median_seconds_source=source, totals=totals
    )


# ====================================================================================================
# Quality (ADR-0010 metrics, trend charts, acceptance rate, benchmark reports, upgrade rule)
# ====================================================================================================


def _snapshot_series(session: Session, column: str, *, limit: int = 20) -> list[SnapshotPoint]:
    rows = session.execute(
        text(
            f"SELECT at, {column} FROM metrics_snapshots WHERE scope = 'ingestion_run' "
            f"ORDER BY at DESC LIMIT :n"
        ),
        {"n": limit},
    ).all()
    points = [SnapshotPoint(at=r[0], value=(float(r[1]) if r[1] is not None else None)) for r in rows]
    points.reverse()  # chronological, oldest first, for the trend line
    return points


def _acceptance(session: Session) -> tuple[int, int, int, int, float | None]:
    rows = session.execute(
        text("SELECT verdict, count(*) FROM extraction_reviews GROUP BY verdict")
    ).all()
    counts = {str(v): int(c) for v, c in rows}
    accept, wrong, partial = counts.get("accept", 0), counts.get("wrong", 0), counts.get("partial", 0)
    total = accept + wrong + partial
    rate = round(100.0 * accept / total, 1) if total else None
    return total, accept, wrong, partial, rate


def _upgrade_notice(session: Session) -> str | None:
    """ADR-0010 rule: acceptance < 70% over two consecutive review batches, or validity < 85% over
    two runs. Both checks need at least two data points; fewer than that is silence, not a false
    all-clear."""
    batches = session.execute(
        text(
            """
            SELECT sample_batch,
                   count(*) FILTER (WHERE verdict = 'accept')::float / count(*) AS rate,
                   max(at) AS at
              FROM extraction_reviews
             WHERE sample_batch IS NOT NULL
             GROUP BY sample_batch
             ORDER BY at DESC
             LIMIT 2
            """
        )
    ).all()
    if len(batches) == 2 and all(r[1] is not None and r[1] < 0.70 for r in batches):
        return (
            f"Acceptance rate was below 70% in the last two review batches "
            f"({batches[0][1] * 100:.0f}%, {batches[1][1] * 100:.0f}%) - ADR-0010 says the owner "
            f"should be told and a candidate model benchmarked."
        )
    validity = session.execute(
        text(
            "SELECT schema_validity_rate FROM metrics_snapshots "
            "WHERE scope = 'ingestion_run' AND schema_validity_rate IS NOT NULL "
            "ORDER BY at DESC LIMIT 2"
        )
    ).all()
    if len(validity) == 2 and all(r[0] is not None and r[0] < 0.85 for r in validity):
        return (
            f"JSON-schema validity was below 85% in the last two runs "
            f"({validity[0][0] * 100:.0f}%, {validity[1][0] * 100:.0f}%) - ADR-0010 says the owner "
            f"should be told and a candidate model benchmarked."
        )
    return None


def quality_view(session: Session) -> QualityView:
    latest_at = session.execute(
        text("SELECT max(at) FROM metrics_snapshots WHERE scope = 'ingestion_run'")
    ).scalar()
    total, accept, wrong, partial, rate = _acceptance(session)
    reports = sorted(p.name for p in REPORTS_DIR.glob("benchmark-*.md")) if REPORTS_DIR.is_dir() else []
    return QualityView(
        has_snapshots=latest_at is not None,
        latest_at=latest_at,
        schema_validity_rate=_snapshot_series(session, "schema_validity_rate"),
        failed_episode_share=_snapshot_series(session, "failed_episode_share"),
        median_seconds_per_episode=_snapshot_series(session, "median_seconds_per_episode"),
        duplicate_entity_rate=_snapshot_series(session, "duplicate_entity_rate"),
        unconfirmed_fact_share=_snapshot_series(session, "unconfirmed_fact_share"),
        review_total=total,
        review_accept=accept,
        review_wrong=wrong,
        review_partial=partial,
        acceptance_rate=rate,
        benchmark_reports=reports,
        upgrade_notice=_upgrade_notice(session),
    )


# ====================================================================================================
# Review queue (ADR-0010 human review: accept | wrong | partial -> extraction_reviews)
# ====================================================================================================


def _citation(row: dict[str, object]) -> str:
    prov = Provenance(
        source_id=row.get("source_id"),  # type: ignore[arg-type]
        source_uri=row.get("source_uri"),  # type: ignore[arg-type]
        source_hash=row.get("source_hash"),  # type: ignore[arg-type]
        device_id="local-development-machine",
        observed_at=row.get("observed_at") or utc_now(),  # type: ignore[arg-type]
    )
    return prov.citation()


def review_queue_view(session: Session, *, sample: int = 20) -> ReviewQueueView:
    """``N`` recent artifacts/facts that have **no** verdict yet, newest first.

    Facts carry no ``evidence_quote`` column (only artifacts do); the natural-language ``statement``
    is shown in its place and labelled, never the raw source text (plan section T).
    """
    half = max(1, sample // 2)
    artifact_rows = session.execute(
        text(
            """
            SELECT a.id, a.title, a.evidence_quote, a.source_uri, a.source_hash, a.project_id,
                   a.observed_at, a.episode_id, a.extraction_model_id
              FROM knowledge_artifacts a
              LEFT JOIN extraction_reviews r
                     ON r.object_type = 'artifact' AND r.object_id = a.id
             WHERE r.id IS NULL
             ORDER BY a.observed_at DESC
             LIMIT :n
            """
        ),
        {"n": half},
    ).all()
    fact_rows = session.execute(
        text(
            """
            SELECT f.id, f.statement, f.source_uri, f.source_hash, f.project_id,
                   f.observed_at, f.episode_id, f.extraction_model_id
              FROM facts f
              LEFT JOIN extraction_reviews r
                     ON r.object_type = 'fact' AND r.object_id = f.id
             WHERE r.id IS NULL
             ORDER BY f.observed_at DESC
             LIMIT :n
            """
        ),
        {"n": sample - half},
    ).all()

    candidates: list[ReviewCandidate] = []
    for r in artifact_rows:
        candidates.append(
            ReviewCandidate(
                object_type="artifact",
                object_id=r[0],
                headline=r[1],
                evidence=r[2],
                citation=_citation({"source_uri": r[3], "source_hash": r[4], "observed_at": r[6]}),
                project_id=r[5],
                observed_at=r[6],
                episode_id=r[7],
                model_id=r[8],
            )
        )
    for r in fact_rows:
        candidates.append(
            ReviewCandidate(
                object_type="fact",
                object_id=r[0],
                headline=r[1],
                evidence=None,
                citation=_citation({"source_uri": r[2], "source_hash": r[3], "observed_at": r[5]}),
                project_id=r[4],
                observed_at=r[5],
                episode_id=r[6],
                model_id=r[7],
            )
        )
    candidates.sort(key=lambda c: c.observed_at or utc_now(), reverse=True)

    today_count = int(
        session.execute(
            text(
                "SELECT count(*) FROM extraction_reviews WHERE at >= date_trunc('day', now())"
            )
        ).scalar()
        or 0
    )
    batch = utc_now().strftime("%Y-%m-%d")
    return ReviewQueueView(sample=candidates, sample_batch=batch, already_reviewed_today=today_count)


# ====================================================================================================
# Model usage (llm_calls, migration 0004) - cost, volume, speed and reliability per LLM call
# ====================================================================================================


def usage_view(session: Session, *, recent_limit: int = 20) -> UsageView:
    """Everything the Model usage section shows. ``llm_calls`` may legitimately be empty (recording
    was wired before any extraction ran since) - every branch below degrades to zero/None rather than
    dividing by zero or fabricating a number."""
    totals = session.execute(
        text(
            """
            SELECT count(*) AS total_calls,
                   coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
                   coalesce(sum(completion_tokens), 0) AS completion_tokens,
                   sum(cost_usd) AS cost_usd,
                   count(*) FILTER (WHERE cost_usd IS NULL) AS unpriced,
                   count(*) FILTER (WHERE NOT ok) AS errors,
                   count(*) FILTER (WHERE attempts > 1) AS retried
              FROM llm_calls
            """
        )
    ).first()
    assert totals is not None  # a bare aggregate always returns exactly one row

    total_calls = int(totals[0] or 0)
    total_cost = float(totals[3]) if totals[3] is not None else None
    errors = int(totals[5] or 0)
    retried = int(totals[6] or 0)

    model_rows = session.execute(
        text(
            """
            SELECT model_id, provider, count(*) AS calls,
                   coalesce(sum(prompt_tokens), 0) AS prompt_tokens,
                   coalesce(sum(completion_tokens), 0) AS completion_tokens,
                   sum(cost_usd) AS cost_usd,
                   count(*) FILTER (WHERE cost_usd IS NULL) AS unpriced
              FROM llm_calls
             GROUP BY model_id, provider
             ORDER BY calls DESC, model_id
            """
        )
    ).all()
    by_model = [
        ModelUsageRow(
            provider=r[1],
            model_id=r[0],
            calls=int(r[2]),
            prompt_tokens=int(r[3] or 0),
            completion_tokens=int(r[4] or 0),
            cost_usd=(float(r[5]) if r[5] is not None else None),
            unpriced_calls=int(r[6] or 0),
        )
        for r in model_rows
    ]

    purpose_rows = session.execute(
        text(
            """
            SELECT purpose,
                   count(*) AS n,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50,
                   percentile_cont(0.9) WITHIN GROUP (ORDER BY duration_ms) AS p90,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95,
                   sum(completion_tokens) AS completion_tokens,
                   sum(duration_ms) AS duration_ms_sum
              FROM llm_calls
             WHERE duration_ms IS NOT NULL
             GROUP BY purpose
             ORDER BY purpose
            """
        )
    ).all()
    by_purpose = [
        PurposeLatencyRow(
            purpose=r[0],
            count=int(r[1]),
            median_ms=(float(r[2]) if r[2] is not None else None),
            p90_ms=(float(r[3]) if r[3] is not None else None),
            p95_ms=(float(r[4]) if r[4] is not None else None),
            tokens_per_second=(
                round(float(r[5]) / (float(r[6]) / 1000.0), 2)
                if r[5] and r[6]
                else None
            ),
        )
        for r in purpose_rows
    ]

    # Cost tied to value: the denominator is restricted to exactly the episodes that have a
    # recorded call, and the facts extracted from exactly those episodes - never the whole
    # historical corpus, most of which predates this table (see UsageView docstring).
    documents_priced = int(
        session.execute(
            text("SELECT count(DISTINCT episode_id) FROM llm_calls WHERE episode_id IS NOT NULL")
        ).scalar()
        or 0
    )
    facts_priced = int(
        session.execute(
            text(
                """
                SELECT count(*) FROM facts
                 WHERE episode_id IN (
                     SELECT DISTINCT episode_id FROM llm_calls WHERE episode_id IS NOT NULL
                 )
                """
            )
        ).scalar()
        or 0
    )
    cost_per_document = (
        round(total_cost / documents_priced, 4) if total_cost is not None and documents_priced else None
    )
    cost_per_fact = (
        round(total_cost / facts_priced, 5) if total_cost is not None and facts_priced else None
    )

    recent_rows = session.execute(
        text(
            """
            SELECT at, provider, model_id, purpose, prompt_tokens, completion_tokens, duration_ms,
                   cost_usd, ok, attempts
              FROM llm_calls
             ORDER BY at DESC
             LIMIT :n
            """
        ),
        {"n": recent_limit},
    ).all()
    recent = [
        RecentCallRow(
            at=r[0],
            provider=r[1],
            model_id=r[2],
            purpose=r[3],
            prompt_tokens=r[4],
            completion_tokens=r[5],
            duration_ms=r[6],
            cost_usd=(float(r[7]) if r[7] is not None else None),
            ok=bool(r[8]),
            attempts=int(r[9]),
        )
        for r in recent_rows
    ]

    return UsageView(
        has_calls=total_calls > 0,
        total_calls=total_calls,
        total_prompt_tokens=int(totals[1] or 0),
        total_completion_tokens=int(totals[2] or 0),
        total_cost_usd=total_cost,
        unpriced_calls=int(totals[4] or 0),
        by_model=by_model,
        documents_priced=documents_priced,
        facts_priced=facts_priced,
        cost_per_document=cost_per_document,
        cost_per_fact=cost_per_fact,
        by_purpose=by_purpose,
        error_rate_pct=(round(100.0 * errors / total_calls, 1) if total_calls else None),
        retry_rate_pct=(round(100.0 * retried / total_calls, 1) if total_calls else None),
        recent=recent,
        ttft_note=_TTFT_NOTE,
    )


# ====================================================================================================
# Attention (things a person can act on, not restated counters)
# ====================================================================================================


_NOTES_SQL = """
    SELECT e.observed_at                                        AS at,
           e.type                                               AS kind,
           e.status                                             AS status,
           e.project_id                                         AS project_id,
           coalesce(NULLIF(e.title, ''), left(e.body, 90))      AS title,
           s.uri                                                AS source_uri,
           (SELECT count(*) FROM facts f WHERE f.episode_id = e.id)               AS facts,
           (SELECT count(*) FROM knowledge_artifacts a WHERE a.episode_id = e.id) AS artifacts,
           EXISTS (SELECT 1 FROM chunks c WHERE c.source_id = e.source_id)        AS searchable
      FROM episodes e
 LEFT JOIN sources s ON s.id = e.source_id
     WHERE e.type IN ('manual', 'mcp')
        OR s.relative_path LIKE :session_path
  ORDER BY e.observed_at DESC
     LIMIT :limit
"""

#: Vault notes this project writes about its own sessions. They arrive as ordinary `document`
#: episodes, so nothing distinguishes them from any other file except where they live.
SESSION_NOTE_PATH = "01 Projects/AI Memory/%"


def notes_view(session: Session, *, limit: int = 50) -> NotesView:
    """Knowledge that did not come from a file the system discovered by itself.

    Nothing else in the system answers "what have I saved?". `/v1/sources` lists files, and
    `memory_search` needs embeddings - which an MCP write never produces, because `add_episode`
    writes an episode and no chunks. So a session recorded through MCP is real, extracted knowledge
    that no text search can reach, and without this panel the only way to know it exists is SQL.

    `searchable` is the column that matters: it says whether the *words* can be found, not just the
    facts drawn from them.
    """
    rows = (
        session.execute(text(_NOTES_SQL), {"limit": limit, "session_path": SESSION_NOTE_PATH})
        .mappings()
        .all()
    )
    out: list[NoteRow] = []
    for row in rows:
        kind = "session note" if row["kind"] == "document" else str(row["kind"])
        out.append(
            NoteRow(
                at=row["at"],
                kind=kind,
                title=(row["title"] or "(untitled)").strip(),
                project_id=row["project_id"],
                status=str(row["status"]),
                facts=int(row["facts"] or 0),
                artifacts=int(row["artifacts"] or 0),
                searchable=bool(row["searchable"]),
                source_uri=row["source_uri"],
            )
        )
    return NotesView(
        rows=out,
        total=len(out),
        unsearchable=sum(1 for r in out if not r.searchable),
    )


def attention_view(session: Session) -> AttentionView:
    items: list[AttentionItem] = []

    pending_row = session.execute(
        text("SELECT count(*), min(created_at) FROM episodes WHERE status = 'pending'")
    ).one()
    pending = int(pending_row[0] or 0)
    pending_first_observed = pending_row[1]
    total_episodes = int(session.execute(text("SELECT count(*) FROM episodes")).scalar() or 0)
    if pending:
        items.append(
            AttentionItem(
                severity="notice",
                title=f"{pending} of {total_episodes} episodes are still unextracted",
                detail=(
                    "Tier 2 (LLM knowledge extraction) has not run on these yet - use Run scan below "
                    "to queue them. This is the single most useful number on this page."
                ),
                first_observed_at=pending_first_observed,
            )
        )

    unembeddable = ingest_repo.unembeddable_sources(session, limit=25)
    if unembeddable:
        unembeddable_first_seen = session.execute(
            text(
                "SELECT min(s.first_seen_at) FROM sources s "
                "WHERE s.policy IN ('INDEX_CONTENT', 'MIRROR') "
                "AND s.status <> 'deleted' "
                "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.source_id = s.id)"
            )
        ).scalar()
        items.append(
            AttentionItem(
                severity="info",
                title=f"{len(unembeddable)} INDEX_CONTENT/MIRROR source(s) produced no chunk",
                detail="Settled outcomes, not pending retries - each one has a named reason:",
                first_observed_at=unembeddable_first_seen,
                first_observed_label="Earliest source first observed",
                items=[f"{u['path']} ({u['root_id']}): {u['reason']}" for u in unembeddable],
            )
        )

    fallback_rows = session.execute(
        text(
            """
                        SELECT relative_path, root_id, policy_reason, min(first_seen_at) OVER ()
              FROM sources
             WHERE policy = 'CATALOG_ONLY'
               AND status <> 'deleted'
               AND policy_reason IS NOT NULL
               AND policy_reason NOT LIKE 'policies.yaml:%'
             ORDER BY relative_path
             LIMIT 25
            """
        )
    ).all()
    if fallback_rows:
        items.append(
            AttentionItem(
                severity="warning",
                title=f"{len(fallback_rows)} source(s) fell back to CATALOG_ONLY at runtime",
                detail=(
                    "Not excluded by config (policies.yaml) - these hit an extractor failure, a "
                    "duplicate, or the secret detector:"
                ),
                first_observed_at=fallback_rows[0][3],
                first_observed_label="Earliest source first observed",
                items=[f"{r[0]} ({r[1]}): {r[2]}" for r in fallback_rows],
            )
        )

    secret_rows = session.execute(
        text(
            "SELECT relative_path, root_id, policy_reason FROM sources "
            "WHERE secret_suspected AND status <> 'deleted' ORDER BY relative_path"
        )
    ).all()
    if secret_rows:
        items.append(
            AttentionItem(
                severity="warning",
                title=f"{len(secret_rows)} source(s) suspected to contain a secret",
                detail="Catalogued only; content was never chunked, embedded or shown on this page.",
                items=[f"{r[0]} ({r[1]}): {r[2] or 'secret detector matched'}" for r in secret_rows],
            )
        )

    deleted_rows = session.execute(
        text(
            "SELECT relative_path, root_id FROM sources WHERE status = 'deleted' "
            "ORDER BY last_seen_at DESC LIMIT 25"
        )
    ).all()
    deleted_total = int(
        session.execute(text("SELECT count(*) FROM sources WHERE status = 'deleted'")).scalar() or 0
    )
    if deleted_rows:
        items.append(
            AttentionItem(
                severity="info",
                title=f"{deleted_total} source(s) deleted from disk (knowledge kept, per ADR-0005)",
                detail="Facts and artifacts derived from these are marked source_status=deleted, not removed:",
                items=[f"{r[0]} ({r[1]})" for r in deleted_rows],
            )
        )

    unconfirmed_rows = session.execute(
        text(
            """
            SELECT f.statement, f.project_id
              FROM facts f
             WHERE f.status = 'unconfirmed'
             ORDER BY f.observed_at DESC
             LIMIT 25
            """
        )
    ).all()
    unconfirmed_total = int(
        session.execute(text("SELECT count(*) FROM facts WHERE status = 'unconfirmed'")).scalar() or 0
    )
    if unconfirmed_rows:
        items.append(
            AttentionItem(
                severity="notice",
                title=f"{unconfirmed_total} fact(s) unconfirmed (not re-observed in the newest source version)",
                detail="Still current, ranked down in search until re-confirmed or superseded:",
                items=[f"{r[0]} ({r[1] or '(no project)'})" for r in unconfirmed_rows],
            )
        )

    no_quote = int(
        session.execute(
            text("SELECT count(*) FROM knowledge_artifacts WHERE evidence_quote IS NULL")
        ).scalar()
        or 0
    )
    artifact_total = int(session.execute(text("SELECT count(*) FROM knowledge_artifacts")).scalar() or 0)
    if no_quote:
        items.append(
            AttentionItem(
                severity="notice",
                title=f"{no_quote} of {artifact_total} artifacts have no evidence quote",
                detail=(
                    "The artifact stands on its citation alone; review these first in the Review "
                    "queue below."
                ),
            )
        )

    denied_rows = session.execute(
        text(
            "SELECT denied_reason, count(*) FROM mcp_audit_log WHERE NOT allowed "
            "GROUP BY denied_reason ORDER BY 2 DESC"
        )
    ).all()
    if denied_rows:
        total_denied = sum(int(c) for _, c in denied_rows)
        items.append(
            AttentionItem(
                severity="info",
                title=f"{total_denied} write attempt(s) refused and logged",
                detail="mcp_audit_log; every refusal is recorded, none of them wrote anything:",
                items=[f"{reason or '(no reason recorded)'}: {count}" for reason, count in denied_rows],
            )
        )

    notice = _upgrade_notice(session)
    if notice:
        items.append(AttentionItem(severity="warning", title="Model-upgrade rule triggered (ADR-0010)", detail=notice))

    return AttentionView(items=items)
