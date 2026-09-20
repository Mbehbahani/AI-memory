"""The always-on ingestion worker (A07a, ADR-0011 / plan section AG).

The compose ``ingestion`` service runs ``aimemory-ingest worker``. It polls the ``run_requests`` table
(the only thing the Ops page and the CLI can enqueue), executes one request at a time, updates
``progress_pct``/``message`` as it goes, and writes a ``metrics_snapshots`` row plus a
``service_stats`` row after each run (plan section AF).

Containers never get the Docker socket (ADR-0011 point 3), so the worker's whole vocabulary is the
:class:`~aimemory.domain.enums.RunAction` enum: ``scan``, ``retry_failed``, ``eval``, ``benchmark``.
``eval``/``benchmark`` belong to A12's ``scripts/eval``; the worker records them as unsupported here
rather than pretending to run them.

RAM: ``docker stats`` is not readable from inside a container, so the worker records its **own**
process RSS (``/proc/self/status`` on Linux, unavailable elsewhere -> ``None``) and Ollama's
``/api/ps`` model-loaded flag. Host-level RAM stays the job of ``scripts/doctor`` - labelled, per
ADR-0011.
"""

from __future__ import annotations

import os
import signal
import statistics
import time
from collections.abc import Sequence
from types import FrameType
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text

from ..common.config import Settings, get_settings
from ..common.ids import new_id
from ..common.logging import get_logger
from ..common.time import utc_now
from ..domain.enums import JobStage, RunAction, RunTrigger, Tier
from ..domain.models import MetricsSnapshot, ServiceStat, SourceRoot
from ..persistence import ingest_repo
from ..persistence.repositories import MetricsRepo
from .freshness import write_freshness_stat
from .pipeline import IngestionPipeline, SessionScope
from .roots import RootContext, build_root_context, load_source_roots

#: How often the idle worker re-compares the source folders against the database. Two minutes is
#: chosen against how fast the underlying fact can change: it changes when a person saves a file.
FRESHNESS_INTERVAL_SECONDS = 120.0

__all__ = [
    "FRESHNESS_INTERVAL_SECONDS",
    "IngestionWorker",
    "process_request",
    "write_metrics_snapshot",
    "write_service_stat",
]

logger = get_logger(__name__)


# --------------------------------------------------------------------------------------------------
# Metrics (plan section AF)
# --------------------------------------------------------------------------------------------------


def _process_rss_bytes() -> int | None:
    """Own RSS from ``/proc/self/status``; ``None`` where that file does not exist (Windows host)."""
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        return None
    return None


def _ollama_model_loaded(settings: Settings) -> tuple[bool, str | None]:
    """``/api/ps`` - is a model resident right now, and which one."""
    try:
        response = httpx.get(f"{settings.llm.ollama_url}/api/ps", timeout=3.0)
        models = response.json().get("models") or []
    except Exception:  # noqa: BLE001 - the worker must never die because Ollama is busy
        return False, None
    if not models:
        return False, None
    return True, str(models[0].get("name") or models[0].get("model") or "")


def write_service_stat(scope: SessionScope, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    loaded, model_name = _ollama_model_loaded(settings)
    with scope.session() as session:
        MetricsRepo(session).record_service_stat(
            ServiceStat(
                id=new_id(),
                at=utc_now(),
                service="ingestion",
                process_rss_bytes=_process_rss_bytes(),
                model_loaded=loaded,
                model_name=model_name,
                details={"llm_provider": settings.llm.provider},
            )
        )


def _snapshot_model_id(scope: SessionScope, settings: Settings) -> str | None:
    """The ``extraction_models.id`` to stamp this snapshot with, or ``None`` when there isn't one.

    ``metrics_snapshots.model_id`` is a foreign key onto ``extraction_models``. It must therefore be
    an id that table actually holds - not a raw provider model name. This function used to be
    ``settings.llm.model`` inline, which is the name a person writes in ``.env`` (``qwen3:4b``,
    ``us.anthropic.claude-haiku-...``) and never the registered id (``qwen3-4b``, ``bedrock:us.an...``).
    MEASURED: every scan after the Bedrock switch ended
    ``ForeignKeyViolation ... Key (model_id)=(qwen3:4b) is not present in table "extraction_models"``,
    reported on the Ops page as a failed run even though the scan itself had already completed.

    Resolution goes through :func:`aimemory.sources.tier2.resolve_extraction_model_id`, the same
    function the extraction path uses, so the snapshot names the model that actually did the work and
    follows a provider change automatically. The id is then *verified to exist* rather than assumed:
    a Tier-1-only scan never calls the LLM, so nothing has registered the identity yet, and stamping
    an unregistered id would fail the FK again. ``None`` is the honest answer there - the column is
    nullable precisely for "this scope did not attribute to a model".
    """
    from .tier2 import resolve_extraction_model_id  # noqa: PLC0415 - avoids an import cycle

    try:
        model_id = resolve_extraction_model_id(settings)
    except Exception as exc:  # noqa: BLE001 - an unreachable provider must not fail a finished scan
        logger.warning("worker.snapshot_model_unresolved", error=f"{type(exc).__name__}: {exc}"[:200])
        return None
    with scope.session() as session:
        known = session.execute(
            text("SELECT 1 FROM extraction_models WHERE id = :id"), {"id": model_id}
        ).first()
    if known is None:
        logger.info("worker.snapshot_model_unregistered", model_id=model_id)
        return None
    return model_id


def write_metrics_snapshot(
    scope: SessionScope, *, run_id: UUID | None = None, settings: Settings | None = None
) -> MetricsSnapshot | None:
    """Write one ``metrics_snapshots`` row from the current database state (plan section AF)."""
    settings = settings or get_settings()
    with scope.session() as session:
        report = ingest_repo.status_report(session)
        durations = ingest_repo.recent_stage_durations_ms(session, JobStage.EXTRACT_KNOWLEDGE)
    episodes = report.episodes_by_status
    total_episodes = sum(episodes.values())
    failed_share = (episodes.get("failed", 0) / total_episodes) if total_episodes else None
    coverage = {
        row.project_id: round(row.extraction_coverage, 4)
        for row in report.coverage
        if row.episodes_total
    }
    snapshot = MetricsSnapshot(
        id=new_id(),
        at=utc_now(),
        scope="ingestion_run",
        run_id=run_id,
        model_id=_snapshot_model_id(scope, settings),
        failed_episode_share=failed_share,
        median_seconds_per_episode=(
            statistics.median(durations) / 1000.0 if durations else None
        ),
        coverage_by_project=coverage,
        extra={
            "sources_active": float(report.sources_by_status.get("active", 0)),
            "chunks": float(report.chunks),
            "embeddings": float(report.embeddings),
            "chunks_without_embedding": float(report.chunks_without_embedding),
        },
    )
    with scope.session() as session:
        return MetricsRepo(session).insert_snapshot(snapshot)


# --------------------------------------------------------------------------------------------------
# Request execution
# --------------------------------------------------------------------------------------------------


def _contexts(
    roots: Sequence[SourceRoot], root_id: str | None, settings: Settings
) -> list[RootContext]:
    selected = [r for r in roots if root_id is None or r.root_id == root_id]
    return [build_root_context(root, settings=settings) for root in selected]


def process_request(
    scope: SessionScope, request: Any, *, settings: Settings | None = None
) -> dict[str, Any]:
    """Execute one ``run_requests`` row. Returns a small result dict for the log/message column."""
    settings = settings or get_settings()
    pipeline = IngestionPipeline(scope, settings=settings)
    action = RunAction(request.action)
    result: dict[str, Any] = {"action": action.value}

    if action is RunAction.SCAN:
        roots = load_source_roots(settings=settings)
        pipeline.sync_roots(roots)
        contexts = _contexts(roots, request.root_id, settings)
        tier = Tier(int(request.tier))
        counters: dict[str, int] = {}
        for index, ctx in enumerate(contexts):
            def _progress(done: int, total: int, root_id: str, _i: int = index) -> None:
                share = (_i + (done / total if total else 1)) / max(1, len(contexts))
                with scope.session() as session:
                    ingest_repo.update_run_request_progress(
                        session,
                        request.id,
                        progress_pct=int(share * 100),
                        message=f"{root_id}: {done}/{total}",
                    )

            report = pipeline.run_root(
                ctx,
                tier=tier,
                trigger=RunTrigger.OPS_PAGE
                if request.requested_by == "ops-page"
                else RunTrigger.WORKER,
                requested_by=request.requested_by,
                progress=_progress,
            )
            for key, value in report.counters.items():
                counters[key] = counters.get(key, 0) + value
            result.setdefault("runs", []).append(str(report.run_id))
        result["counters"] = counters
        # The scan is finished and committed by this point. A metrics row is bookkeeping *about*
        # that work, so a failure here is reported, not raised - otherwise the request is marked
        # `failed` on the Ops page for a scan that actually succeeded, which is exactly what the
        # `model_id` foreign-key violation did. Same rule as `knowledge/telemetry.record_call`:
        # telemetry never breaks the thing it measures.
        try:
            write_metrics_snapshot(scope, settings=settings)
        except Exception as exc:  # noqa: BLE001 - see above
            logger.warning(
                "worker.metrics_snapshot_failed", error=f"{type(exc).__name__}: {exc}"[:300]
            )
            result["metrics_snapshot"] = f"not written: {type(exc).__name__}"
        return result

    if action is RunAction.RETRY_FAILED:
        with scope.session() as session:
            jobs = ingest_repo.requeue_failed_jobs(session, root_id=request.root_id)
            episodes = ingest_repo.queue_failed_episodes(session, root_id=request.root_id)
        result["requeued_jobs"] = jobs
        result["requeued_episodes"] = episodes
        return result

    # eval / benchmark are A12's scripts (ADR-0010); the worker does not shell out to them in V0.1.
    raise NotImplementedError(
        f"action '{action.value}' is run by scripts/eval, not by the ingestion worker (ADR-0010)"
    )


class IngestionWorker:
    """Poll ``run_requests`` forever (or ``max_requests`` times, which is what the tests use)."""

    def __init__(
        self,
        scope: SessionScope,
        *,
        settings: Settings | None = None,
        poll_seconds: float | None = None,
    ) -> None:
        self._scope = scope
        self._settings = settings or get_settings()
        self._poll = poll_seconds or self._settings.ingest.worker_poll_seconds
        self._stopping = False
        #: Monotonic timestamp of the last freshness check. `-inf` forces one on the first idle loop
        #: rather than after the first interval, so a freshly started worker shows a real number on
        #: the Ops page immediately instead of "never checked".
        self._last_freshness = float("-inf")

    def request_stop(self, *_args: object) -> None:
        self._stopping = True

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                pass

    def _on_signal(self, _signum: int, _frame: FrameType | None) -> None:
        logger.info("worker.stopping")
        self._stopping = True

    def _maybe_check_freshness(self, *, force: bool = False) -> None:
        """Compare the source folders against the database, at most every ``FRESHNESS_INTERVAL``.

        Throttled because this is the only part of the worker that touches the filesystem when there
        is no work to do. At the poll interval it would stat every file several times a minute for a
        number that changes when a human saves a file - minutes apart at best. The check itself is
        cheap (stat first, hash only the few candidates), but cheap times constant is not free.

        Deliberately runs on the **idle** path, never before a request: a scan is about to make the
        answer obsolete anyway, and the check should not delay the work the operator asked for.
        """
        now = time.monotonic()
        if not force and (now - self._last_freshness) < FRESHNESS_INTERVAL_SECONDS:
            return
        self._last_freshness = now
        write_freshness_stat(self._scope, self._settings)

    def _sync_root_config(self) -> None:
        """Write ``config/source-roots.yaml`` into ``source_roots`` on startup. Never raises.

        Closes a drift that is invisible until it matters. ``sync_source_roots`` otherwise runs only
        inside a scan and from ``aimemory-ingest roots --sync``, so **disabling** a root never reached
        the table: the edit stops the scans that would have recorded it. The scanner reads the YAML
        and behaved correctly; every reader of the table - including
        :func:`aimemory.retrieval.staleness.staleness_warnings`, which exists precisely to flag a
        frozen root - went on seeing ``enabled = true``.

        MEASURED 2026-09-19: ``joblab-de`` was disabled in config on the 18th and the table still
        read ``enabled`` a day later, so search could not warn about the one root nothing was
        watching.

        Startup is the right moment: config can only change while the process is down.
        """
        try:
            # include_disabled=True is the whole point: a *disable* is the change that could not
            # record itself before, and the default filter would drop exactly those rows.
            roots = load_source_roots(settings=self._settings, include_disabled=True)
        except Exception as exc:  # noqa: BLE001 - a bad config must not stop the queue
            logger.warning("worker.root_config_unreadable", error=f"{type(exc).__name__}: {exc}"[:200])
            return
        try:
            with self._scope.session() as session:
                written = ingest_repo.sync_source_roots(session, roots)
            logger.info("worker.root_config_synced", roots=written)
        except Exception as exc:  # noqa: BLE001
            logger.warning("worker.root_config_sync_failed", error=f"{type(exc).__name__}: {exc}"[:200])

    def run_once(self) -> bool:
        """Claim and execute at most one request. Returns ``True`` when one was executed."""
        with self._scope.session() as session:
            request = MetricsRepo(session).claim_next_run_request()
        if request is None:
            self._maybe_check_freshness()
            return False
        logger.info("worker.request_started", action=request.action, root=request.root_id)
        try:
            result = process_request(self._scope, request, settings=self._settings)
            with self._scope.session() as session:
                ingest_repo.finish_run_request(
                    session, request.id, message=_short(result), run_id=_first_run(result)
                )
            logger.info("worker.request_done", action=request.action)
            # A completed scan is exactly when the answer changes - usually to zero. Re-check now so
            # the page stops saying "3 files changed" the moment those 3 files have been picked up.
            self._maybe_check_freshness(force=True)
        except Exception as exc:  # noqa: BLE001 - a bad request must not kill the worker
            message = f"{type(exc).__name__}: {exc}"
            with self._scope.session() as session:
                ingest_repo.fail_run_request(session, request.id, error=message)
            logger.warning("worker.request_failed", action=request.action, error=message[:300])
        write_service_stat(self._scope, self._settings)
        return True

    def run_forever(self, *, max_requests: int | None = None, max_seconds: float | None = None) -> int:
        """Poll until stopped. ``max_requests``/``max_seconds`` exist so tests can bound the loop."""
        self.install_signal_handlers()
        started = time.monotonic()
        handled = 0
        self._sync_root_config()
        write_service_stat(self._scope, self._settings)
        logger.info("worker.started", poll_seconds=self._poll, pid=os.getpid())
        while not self._stopping:
            if max_requests is not None and handled >= max_requests:
                break
            if max_seconds is not None and (time.monotonic() - started) >= max_seconds:
                break
            if self.run_once():
                handled += 1
                continue
            time.sleep(self._poll)
        logger.info("worker.stopped", handled=handled)
        return handled


def _short(result: dict[str, Any]) -> str:
    counters = result.get("counters") or {}
    interesting = {
        key: counters[key]
        for key in ("new", "modified", "moved", "deleted", "duplicate", "unchanged")
        if key in counters
    }
    return f"{result.get('action')}: {interesting or result}"[:500]


def _first_run(result: dict[str, Any]) -> UUID | None:
    runs = result.get("runs") or []
    if not runs:
        return None
    try:
        return UUID(str(runs[0]))
    except (ValueError, TypeError):
        return None
