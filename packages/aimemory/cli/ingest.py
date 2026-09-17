"""``aimemory-ingest`` - the ingestion command line (A07a, P6-T03; ADR-0006, ADR-0011).

Commands
--------
``run``        scan one or all roots up to a tier (``--tier 0|1|2``), optionally ``--dry-run``
``scan``       alias of ``run`` kept because plan section AF calls a scan "``scripts/ingest``"
``status``     per-stage counts, coverage per project, failures (plan section AF, Ops page data)
``reprocess``  ``--failed`` re-queues failed jobs and failed episodes; vectors are never discarded;
               ``--re-extract --model <id>`` re-runs extraction for a scope under one model (ADR-0014)
``tier2``      drain the Tier 2 extraction queue serially (``INGEST_LLM_CONCURRENCY=1``)
``worker``     the always-on ``run_requests`` poller the compose ``ingestion`` service runs
``enqueue``    put a request on that queue from the CLI (same path the Ops page uses)
``roots``      show the resolved source-root registry (and sync it into ``source_roots``)
``rebuild-graph``  re-project Postgres into Neo4j (delegates to A08 once P7-T03 exists)
``migrate``    A04's schema migrations, mounted here so one binary does everything

Nothing in this module prints file content, a password or a DSN: failures are reported with
``Settings.postgres.safe_dsn``-style sanitized text only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from ..common.config import get_settings
from ..common.ids import new_id
from ..common.logging import configure_logging, get_logger
from ..domain.enums import RunAction, RunRequestStatus, RunTrigger, Tier
from ..domain.models import RunRequest
from ..persistence import ingest_repo
from ..persistence.db import Database
from ..persistence.repositories import MetricsRepo
from ..sources.pipeline import IngestionPipeline
from ..sources.roots import build_root_context, load_source_roots, resolve_root_path
from .migrate import migrate as _run_migrations

__all__ = ["app", "main"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="AI Memory ingestion: scan source roots, report status, run the worker.",
)

logger = get_logger(__name__)


def _database() -> Database:
    return Database()


def run_ingestion(
    root: Path | str,
    *,
    project_id: str | None = None,
    session: Any = None,
    label: str = "mini-vault-fixture",
    root_id: str | None = None,
    scheme: str = "localfs",
    device_id: str | None = None,
    tier: int = 2,
    dry_run: bool = False,
    llm_provider: Any = None,
    engine: Any = None,
    writer: Any = None,
    allow_model_mix: bool = False,
    subpath: str | None = None,
    embedder: Any = None,
    settings: Any = None,
    **_ignored: Any,
) -> Any:
    """Ingest one ad-hoc directory as a source root. The seam the scenario suite calls.

    ``config/source-roots.yaml`` describes the *real* roots; a test (or a one-off script) needs to
    point the same pipeline at a throwaway directory - a copy of ``tests/fixtures/mini-vault`` under
    pytest's ``tmp_path`` - without editing that file. Everything else is identical to
    ``aimemory-ingest run``: the same walker, the same change detection, the same job rows.

    ``session`` makes the whole run join a caller-owned transaction (``pg_session`` in the suite, which
    is rolled back at teardown) instead of committing. ``llm_provider`` drains the Tier 2 queue with a
    provider directly - see :func:`aimemory.sources.tier2.run_tier2` for why that path validates and
    records failures but never claims to have extracted knowledge.
    """
    from pathlib import PurePosixPath

    from ..domain.enums import SourceKind, SourceUriScheme, StoragePolicy
    from ..domain.models import Project, SourceRoot
    from ..domain.enums import ProjectStatus, Track
    from ..persistence.repositories import ProjectRepo
    from ..sources.pipeline import single_session_scope

    settings = settings or get_settings()
    base = Path(root)
    source_root = SourceRoot(
        root_id=root_id or label,
        scheme=SourceUriScheme(scheme),
        label=label,
        container_path=PurePosixPath(base.as_posix()),
        device_id=device_id or settings.device_id,
        enabled=True,
        default_policy=StoragePolicy.INDEX_CONTENT,
        default_project_id=project_id,
        kind=SourceKind.DIRECTORY,
        # A vault-shaped directory seeds the Tier 0 registry from its own AIOS files (ADR-0006).
        registry_role="bootstrap" if (base / "AIOS" / "me.md").is_file() else None,
    )
    scope = single_session_scope(session) if session is not None else _database()

    with scope.session() as db_session:
        if project_id and ProjectRepo(db_session).get(project_id) is None:
            ProjectRepo(db_session).upsert(
                Project(
                    id=project_id,
                    name=project_id,
                    track=Track.FOUNDATION,
                    status=ProjectStatus.UNKNOWN,
                    summary="created by run_ingestion() for an ad-hoc root",
                )
            )
        ingest_repo.sync_source_roots(db_session, [source_root])

    ctx = build_root_context(source_root, base_path=base, settings=settings)
    pipeline = IngestionPipeline(scope, settings=settings, embedder=embedder)
    report = pipeline.run_root(
        ctx,
        tier=Tier(tier),
        dry_run=dry_run,
        trigger=RunTrigger.TEST if session is not None else RunTrigger.CLI,
        requested_by="run_ingestion",
        subpath=subpath,
    )
    if (engine is not None or llm_provider is not None) and tier >= int(Tier.KNOWLEDGE) and not dry_run:
        from ..sources.tier2 import run_tier2, run_tier2_with_provider

        if engine is None:
            # An explicit bare provider means "validate responses, persist nothing" and must stay on
            # that path even once A08's engine is importable - otherwise this seam would silently
            # change behaviour the day P8 lands.
            tier2 = run_tier2_with_provider(
                scope,
                llm_provider,
                settings=settings,
                root_id=source_root.root_id,
                allow_model_mix=allow_model_mix,
                run_id=report.run_id,
            )
        else:
            tier2 = run_tier2(
                scope,
                engine=engine,
                writer=writer,
                settings=settings,
                root_id=source_root.root_id,
                allow_model_mix=allow_model_mix,
                run_id=report.run_id,
            )
        report.counters.update(tier2.as_counters())
        # Attached rather than merged: the caller (the scenario suite) asserts on the Tier 2 half
        # separately, and ``counters`` is typed ``dict[str, int]``.
        report.tier2 = tier2
    return report


def _contexts(root: str | None, *, include_disabled: bool = False) -> list[Any]:
    settings = get_settings()
    roots = load_source_roots(settings=settings, include_disabled=include_disabled)
    if root:
        roots = [r for r in roots if r.root_id == root or r.label == root]
        if not roots:
            typer.secho(f"No enabled root named {root!r} in config/source-roots.yaml", fg="red")
            raise typer.Exit(code=2)
    contexts = []
    missing = []
    for item in roots:
        path = resolve_root_path(item)
        if not Path(path).is_dir():
            missing.append((item.root_id, path))
            continue
        contexts.append(build_root_context(item, settings=settings))
    for root_id, path in missing:
        typer.secho(
            f"skipping root '{root_id}': {path} is not mounted in this process", fg="yellow"
        )
    return contexts


@app.command()
def run(
    root: str = typer.Option(None, "--root", help="Root id (vault, joblab-de). Default: all enabled."),
    tier: int = typer.Option(2, "--tier", min=0, max=2, help="0 registry+catalog, 1 +embed, 2 +LLM queue"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Classify only; write nothing."),
    subpath: str = typer.Option(None, "--path", help="Scan only this sub-path of the root."),
    extract: bool = typer.Option(
        False, "--extract", help="Also drain the Tier 2 queue now (serial, slow)."
    ),
    limit: int = typer.Option(None, "--limit", help="With --extract: stop after N episodes."),
    allow_model_mix: bool = typer.Option(
        False,
        "--allow-model-mix",
        help="Benchmarking only (ADR-0014): extract into a corpus that already holds facts from "
        "another model. Never the default; recorded on the run.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the counters as JSON."),
) -> None:
    """Scan roots and apply the plan section L change-detection rules."""
    configure_logging()
    settings = get_settings()
    database = _database()
    pipeline = IngestionPipeline(database, settings=settings)
    all_roots = load_source_roots(settings=settings, include_disabled=True)
    if not dry_run:
        written = pipeline.sync_roots(all_roots)
        typer.echo(f"source_roots synced: {written}")

    totals: dict[str, int] = {}
    for ctx in _contexts(root):
        report = pipeline.run_root(
            ctx,
            tier=Tier(tier),
            dry_run=dry_run,
            trigger=RunTrigger.CLI,
            requested_by="cli",
            subpath=subpath,
        )
        _print_run(report, json_output=json_output)
        for key, value in report.counters.items():
            totals[key] = totals.get(key, 0) + value

    if extract and not dry_run:
        from ..sources.tier2 import ExtractionModelMismatch, run_tier2

        try:
            tier2 = run_tier2(
                database,
                limit=limit,
                settings=settings,
                root_id=root,
                allow_model_mix=allow_model_mix,
            )
        except ExtractionModelMismatch as mismatch:
            typer.secho(f"tier2 refused: {mismatch}", fg="red", err=True)
            raise typer.Exit(code=4) from None
        for note in tier2.notes:
            typer.secho(f"tier2: {note}", fg="yellow")
        typer.echo(
            f"tier2: model={tier2.model_id} processed={tier2.processed} "
            f"extracted={tier2.extracted} failed={tier2.failed} in {tier2.seconds:.1f}s"
        )
        totals.update(tier2.as_counters())
    if json_output:
        typer.echo(json.dumps({"totals": totals}, default=str))


@app.command()
def scan(
    root: str = typer.Option(None, "--root"),
    tier: int = typer.Option(1, "--tier", min=0, max=2),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Alias for ``run`` (plan section AF calls an update a "scan run")."""
    run(
        root=root,
        tier=tier,
        dry_run=dry_run,
        subpath=None,
        extract=False,
        limit=None,
        allow_model_mix=False,
        json_output=False,
    )


@app.command()
def status(
    root: str = typer.Option(None, "--root", help="Restrict source counts to one root."),
    json_output: bool = typer.Option(False, "--json"),
    failures: int = typer.Option(5, "--failures", help="How many recent failures to list."),
) -> None:
    """Per-stage counts, coverage per project, and recent failures (plan section AF)."""
    configure_logging()
    database = _database()
    with database.session() as session:
        report = ingest_repo.status_report(session, root_id=root, failures=failures)
        models_in_use = ingest_repo.extraction_models_in_use(session, root_id=root)
        unembeddable = ingest_repo.unembeddable_sources(session, root_id=root)
    if json_output:
        payload = _status_dict(report)
        payload["extraction_models_in_use"] = models_in_use
        payload["unembeddable_sources"] = unembeddable
        typer.echo(json.dumps(payload, default=str, indent=2))
        return

    typer.secho("Sources", bold=True)
    typer.echo(f"  by status : {report.sources_by_status or '{}'}")
    typer.echo(f"  by policy : {report.sources_by_policy or '{}'}")
    typer.echo(f"  by root   : {report.sources_by_root or '{}'}")
    typer.echo(f"  secret_suspected: {report.secret_suspected}")
    typer.secho("Content", bold=True)
    typer.echo(
        f"  versions={report.versions} texts={report.texts} chunks={report.chunks} "
        f"embeddings={report.embeddings} chunks_without_embedding={report.chunks_without_embedding}"
    )
    typer.secho("Stages (ingestion_jobs)", bold=True)
    if not report.stages:
        typer.echo("  (none yet)")
    for stage, states in sorted(report.stages.items()):
        rendered = " ".join(f"{state}={count}" for state, count in sorted(states.items()))
        typer.echo(f"  {stage:<18} {rendered}")
    typer.secho("Episodes (Tier 2 queue)", bold=True)
    typer.echo(f"  {report.episodes_by_status or '{}'}")
    typer.secho("Coverage per project", bold=True)
    typer.echo(
        f"  {'project':<28} {'indexed':>8} {'embedded':>9} {'embed%':>7} "
        f"{'episodes':>9} {'extracted':>10} {'extract%':>9}"
    )
    totals = [0, 0]
    for row in report.coverage:
        totals[0] += row.sources_indexable
        totals[1] += row.sources_embedded
        typer.echo(
            f"  {row.project_id[:28]:<28} {row.sources_indexable:>8} {row.sources_embedded:>9} "
            f"{row.embed_coverage * 100:>6.1f}% {row.episodes_total:>9} "
            f"{row.episodes_extracted:>10} {row.extraction_coverage * 100:>8.1f}%"
        )
    # The Tier 1 acceptance line (P7-T02) is the total, not any single project row.
    overall = (totals[1] / totals[0] * 100) if totals[0] else 0.0
    typer.echo(f"  {'TOTAL':<28} {totals[0]:>8} {totals[1]:>9} {overall:>6.1f}%")
    # Everything below 100 % is listed by name: a coverage gap with no explanation next to it is
    # indistinguishable from a bug, and these are settled outcomes rather than pending retries.
    if unembeddable:
        typer.secho(
            f"Indexable but nothing to embed ({len(unembeddable)}) - each is a settled outcome",
            bold=True,
            fg="yellow",
        )
        for item in unembeddable:
            typer.echo(f"  [{item['stage']}] {item['path']}: {str(item['reason'])[:90]}")
    typer.secho("Knowledge", bold=True)
    typer.echo(
        f"  projects={report.projects} entities={report.entities} facts={report.facts} "
        f"artifacts={report.artifacts}"
    )
    # ADR-0014 rule 2: more than one entry here means the corpus is mixed and needs a re-extraction.
    typer.echo(f"  extraction models in use: {models_in_use or '{} (no LLM facts yet)'}")
    if len(models_in_use) > 1:
        typer.secho(
            "  WARNING: more than one extraction model has current facts. "
            "Run `aimemory-ingest reprocess --re-extract --model <id>` (ADR-0014).",
            fg="red",
        )
    if report.failures:
        typer.secho("Recent failures", bold=True, fg="red")
        for failure in report.failures:
            typer.echo(f"  [{failure['stage']}] {failure['uri']}: {str(failure['error'])[:120]}")
    if report.last_runs:
        typer.secho("Recent runs", bold=True)
        for entry in report.last_runs:
            typer.echo(
                f"  {entry['started_at']} root={entry['root_id']} tier={entry['tier']} "
                f"{entry['status']}"
            )


@app.command()
def reprocess(
    failed: bool = typer.Option(False, "--failed", help="Re-queue failed jobs and failed episodes."),
    re_extract: bool = typer.Option(
        False,
        "--re-extract",
        help="ADR-0014 remedy: re-run extraction for a scope under ONE model, superseding the old "
        "facts through the normal temporal path (they become historical, never deleted).",
    ),
    model: str = typer.Option(
        None, "--model", help="extraction_models.id to re-extract under. Default: the configured one."
    ),
    project: str = typer.Option(None, "--project", help="Restrict the scope to one project id."),
    root: str = typer.Option(None, "--root"),
    run_now: bool = typer.Option(
        False, "--run", help="With --re-extract: drain the re-queued episodes immediately."
    ),
    limit: int = typer.Option(None, "--limit", help="With --run: stop after N episodes."),
) -> None:
    """Recovery path of plan section L, and the ADR-0014 re-extraction remedy.

    ``--failed`` re-queues failed jobs and episodes; the vectors they already produced are never
    discarded. ``--re-extract --model <id>`` re-queues an entire scope so one model owns it again -
    the only sanctioned way out of a model mismatch, because the alternative (extracting the rest of
    the corpus with a second model) is exactly the mismatch the guard exists to prevent.
    """
    configure_logging()
    if not failed and not re_extract:
        typer.secho("nothing to do: pass --failed or --re-extract", fg="yellow")
        raise typer.Exit(code=1)
    settings = get_settings()
    database = _database()

    if failed:
        with database.session() as session:
            jobs = ingest_repo.requeue_failed_jobs(session, root_id=root)
            episodes = ingest_repo.queue_failed_episodes(session, root_id=root)
        typer.echo(f"re-queued jobs: {jobs}")
        typer.echo(f"re-queued episodes: {episodes}")

    if not re_extract:
        return

    from ..sources.tier2 import ExtractionModelMismatch, resolve_extraction_model_id, run_tier2

    model_id = model or resolve_extraction_model_id(settings)
    with database.session() as session:
        in_use = ingest_repo.extraction_models_in_use(session, project_id=project, root_id=root)
        requeued = ingest_repo.requeue_episodes_for_reextraction(
            session, project_id=project, root_id=root
        )
        flagged = ingest_repo.flag_other_model_facts(
            session, model_id, project_id=project, root_id=root
        )
    scope = project or root or "whole corpus"
    typer.secho(f"re-extraction of {scope} under {model_id}", bold=True)
    typer.echo(f"  models currently in that scope: {in_use or '{}'}")
    typer.echo(f"  episodes re-queued: {requeued}")
    typer.echo(f"  facts from other models flagged unconfirmed (never deleted): {flagged}")
    if not run_now:
        typer.echo("  run `aimemory-ingest tier2` (or repeat with --run) to drain the queue")
        return
    try:
        report = run_tier2(
            database,
            limit=limit,
            settings=settings,
            project_id=project,
            root_id=root,
            model_id=model_id,
            # The guard is deliberately bypassed *here only*: --re-extract is its sanctioned remedy,
            # so the scope is being taken over by one model rather than mixed. The old generation was
            # flagged above and is superseded by the engine through the ADR-0005 path; nothing is
            # deleted, and `allow_model_mix` is still recorded on the run either way.
            allow_model_mix=True,
        )
    except ExtractionModelMismatch as mismatch:  # pragma: no cover - allow_model_mix is set above
        typer.secho(f"refused: {mismatch}", fg="red", err=True)
        raise typer.Exit(code=4) from None
    typer.echo(
        f"  extracted={report.extracted} failed={report.failed} processed={report.processed} "
        f"model={report.model_id}"
    )


@app.command("tier2")
def tier2_command(
    limit: int = typer.Option(None, "--limit", help="Stop after N episodes."),
    project: str = typer.Option(None, "--project", help="Restrict to one project id."),
    root: str = typer.Option(None, "--root", help="Restrict to one source root."),
    allow_model_mix: bool = typer.Option(
        False, "--allow-model-mix", help="Benchmarking only (ADR-0014). Recorded on the run."
    ),
) -> None:
    """Drain the Tier 2 LLM queue serially (ADR-0006); needs A08's engine to be available."""
    configure_logging()
    from ..sources.tier2 import ExtractionModelMismatch, run_tier2

    database = _database()
    try:
        report = run_tier2(
            database,
            limit=limit,
            project_id=project,
            root_id=root,
            allow_model_mix=allow_model_mix,
        )
    except ExtractionModelMismatch as mismatch:
        typer.secho(f"refused: {mismatch}", fg="red", err=True)
        raise typer.Exit(code=4) from None
    for note in report.notes:
        typer.secho(note, fg="yellow")
    typer.echo(
        f"processed={report.processed} extracted={report.extracted} failed={report.failed} "
        f"engine={report.engine} model={report.model_id} seconds={report.seconds:.1f}"
    )
    if report.engine is None:
        raise typer.Exit(code=3)


@app.command()
def worker(
    max_requests: int = typer.Option(None, "--max-requests", help="Exit after N requests (tests)."),
    max_seconds: float = typer.Option(None, "--max-seconds", help="Exit after N seconds (tests)."),
) -> None:
    """ADR-0011: poll ``run_requests`` and execute scans serially. This is the compose command."""
    configure_logging()
    from ..sources.worker import IngestionWorker

    database = _database()
    handled = IngestionWorker(database).run_forever(
        max_requests=max_requests, max_seconds=max_seconds
    )
    typer.echo(f"handled {handled} request(s)")


@app.command()
def enqueue(
    action: str = typer.Option("scan", "--action", help="scan | retry_failed | eval | benchmark"),
    root: str = typer.Option(None, "--root"),
    tier: int = typer.Option(2, "--tier", min=0, max=2),
) -> None:
    """Put a request on the worker's queue (the CLI half of ADR-0011)."""
    configure_logging()
    database = _database()
    with database.session() as session:
        request = MetricsRepo(session).enqueue_run_request(
            RunRequest(
                id=new_id(),
                action=RunAction(action),
                root_id=root,
                tier=Tier(tier),
                requested_by="cli",
                status=RunRequestStatus.QUEUED,
            )
        )
    typer.echo(f"queued {request.action} request {request.id}")


@app.command()
def roots(
    sync: bool = typer.Option(False, "--sync", help="Also write them into the source_roots table."),
) -> None:
    """Show the resolved source-root registry (``config/source-roots.yaml``)."""
    configure_logging()
    settings = get_settings()
    entries = load_source_roots(settings=settings, include_disabled=True)
    for item in entries:
        path = resolve_root_path(item)
        mounted = "mounted" if Path(path).is_dir() else "NOT MOUNTED"
        state = "enabled" if item.enabled else "disabled"
        typer.echo(
            f"{item.root_id:<12} {item.scheme.value}://{item.label}  {path}  [{state}, {mounted}]"
        )
    if sync:
        with _database().session() as session:
            written = ingest_repo.sync_source_roots(session, entries)
        typer.echo(f"source_roots synced: {written}")


@app.command()
def migrate() -> None:
    """Apply A04's Postgres (Alembic) and Neo4j schema migrations. Idempotent.

    Delegates to :func:`aimemory.cli.migrate.migrate` - the plain function A04 exposed exactly so
    this binary could mount it as one command instead of a nested sub-app
    (``docker-compose.yml`` runs ``aimemory-ingest migrate``).
    """
    configure_logging()
    try:
        _run_migrations()
    except Exception as exc:  # surface a clean exit code to the compose service
        typer.secho(f"[migrate] FAILED: {type(exc).__name__}: {exc}", fg="red", err=True)
        raise typer.Exit(code=1) from exc


@app.command("rebuild-graph")
def rebuild_graph() -> None:
    """Re-project Postgres into Neo4j. The projection itself is A08's (P7-T03/P8)."""
    configure_logging()
    try:
        from ..knowledge.structural import rebuild_graph as _rebuild  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        typer.secho(
            "graph projection is not available yet (A08, P7-T03). Nothing was changed.", fg="yellow"
        )
        raise typer.Exit(code=3) from None
    counts = _rebuild()
    typer.echo(json.dumps(counts, default=str))


# --------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------


def _print_run(report: Any, *, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "root": report.root_id,
                    "run_id": str(report.run_id) if report.run_id else None,
                    "tier": int(report.tier),
                    "dry_run": report.dry_run,
                    "counters": report.counters,
                    "errors": report.errors[:10],
                },
                default=str,
            )
        )
        return
    prefix = "DRY RUN " if report.dry_run else ""
    typer.secho(f"{prefix}{report.root_id} (tier {int(report.tier)})", bold=True)
    changes = {
        key: report.counters.get(key, 0)
        for key in ("new", "modified", "moved", "deleted", "duplicate", "unchanged")
    }
    typer.echo("  changes : " + " ".join(f"{k}={v}" for k, v in changes.items()))
    walk = {
        key: report.counters.get(key, 0)
        for key in ("files_seen", "dirs_pruned", "files_ignored", "symlinks_skipped",
                    "path_guard_rejected")
    }
    typer.echo("  walk    : " + " ".join(f"{k}={v}" for k, v in walk.items()))
    written = {
        key: value
        for key, value in report.counters.items()
        if key.startswith(("texts_", "chunks_", "embeddings_", "episodes_", "projects_",
                           "aliases_", "change_", "secret_", "facts_"))
    }
    if written:
        typer.echo("  written : " + " ".join(f"{k}={v}" for k, v in sorted(written.items())))
    if report.errors:
        typer.secho(f"  errors  : {len(report.errors)}", fg="red")
        for error in report.errors[:5]:
            typer.echo(f"    - {error[:160]}")


def _status_dict(report: Any) -> dict[str, Any]:
    return {
        "sources_by_status": report.sources_by_status,
        "sources_by_policy": report.sources_by_policy,
        "sources_by_root": report.sources_by_root,
        "secret_suspected": report.secret_suspected,
        "versions": report.versions,
        "texts": report.texts,
        "chunks": report.chunks,
        "embeddings": report.embeddings,
        "chunks_without_embedding": report.chunks_without_embedding,
        "stages": report.stages,
        "episodes_by_status": report.episodes_by_status,
        "coverage": [
            {
                "project_id": row.project_id,
                "sources_indexable": row.sources_indexable,
                "sources_embedded": row.sources_embedded,
                "episodes_total": row.episodes_total,
                "episodes_extracted": row.episodes_extracted,
                "episodes_failed": row.episodes_failed,
                "embed_coverage": round(row.embed_coverage, 4),
                "extraction_coverage": round(row.extraction_coverage, 4),
            }
            for row in report.coverage
        ],
        "failures": report.failures,
        "last_runs": report.last_runs,
        "projects": report.projects,
        "entities": report.entities,
        "facts": report.facts,
        "artifacts": report.artifacts,
        "generated_at": report.generated_at,
    }


def main() -> None:  # pragma: no cover - console-script entry point
    try:
        app()
    except KeyboardInterrupt:
        typer.echo("interrupted")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    main()
