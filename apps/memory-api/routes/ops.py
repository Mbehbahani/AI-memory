"""``/ops`` - the offline operator dashboard (ADR-0011, plan section AG, P12-T02). Owner: A16.

Server-rendered Jinja2 + HTMX, vendored locally (``static/htmx.min.js`` - no CDN, works with the
browser offline). Seven sections: Health, Runs, Coverage, Quality, Model usage, Review queue,
Attention - each one a partial the full page includes once and HTMX re-fetches on a timer, so the
whole page never needs a client-side framework or a build step.

**A polled partial may not contain a control.** HTMX replaces a section's entire ``innerHTML``, so
anything the operator is part-way through using is destroyed on the next tick. This cost the Runs
section its usability: at ``every 4s`` the root ``<select>`` rebuilt itself under the pointer and the
choice could not be held long enough to submit. Runs is therefore split - ``_runs.html`` (forms,
rendered once, never swapped) and ``_runs_activity.html`` (counters and the request table, polled).
Review never polls at all, for the same reason: every card carries a radio group and a text note.

Model usage reads ``llm_calls`` (migration 0004, one row per LLM call - a document makes 2-3 calls,
entity extraction then relationship extraction, plus an optional retry). ``cost_usd`` is a generated
column on that table; this router only ever reads it, never recomputes it.

Every action button here does exactly one thing: insert a ``queued`` row into ``run_requests``
(:func:`aimemory.ops.actions.enqueue_run`). The always-on ingestion worker (A07a,
``aimemory-ingest worker``) is the only process that ever executes one - this router never runs an
ingest, calls Ollama, or touches Docker, and it is the only place besides the CLI allowed to write to
that table (plan section AG point 3).

The review form writes one ``extraction_reviews`` row per submit
(:func:`aimemory.ops.actions.record_review`) - append-only, same as everywhere else in this system.

Nothing here is behind ``GATEWAY_WRITE_ENABLED`` (that flag is ADR-0008's memory-write gate for
``memory.add_episode``/``memory.record_decision``; ADR-0011 governs this table on its own terms and
says the page and the CLI both create requests). There is no auth in V0.1 (ADR-0011): this is a
loopback-only page for the one operator of this machine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from aimemory.common.logging import get_logger
from aimemory.domain.enums import ObjectType, ReviewVerdict, RunAction, Tier
from aimemory.ops import actions, charts, queries
from aimemory.ops.viewmodels import QualityView
from deps import Runtime, get_runtime
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

logger = get_logger(__name__)

router = APIRouter(tags=["ops"])

_APP_DIR = Path(__file__).resolve().parent.parent
_TEMPLATES_DIR = _APP_DIR / "templates"
_STATIC_DIR = _APP_DIR / "static"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router.mount("/ops/static", StaticFiles(directory=str(_STATIC_DIR)), name="ops-static")


# --------------------------------------------------------------------------------------------------
# Build stamp - "am I looking at the page this server is actually serving?"
# --------------------------------------------------------------------------------------------------
#
# `memory-api` bakes its templates into the image, so a fix only reaches the browser after a rebuild,
# a restart *and* a reload. A tab left open across that sequence keeps polling the endpoints of the
# version it was served, and every fix appears not to have worked - which is exactly what happened:
# a tab polled the retired `/ops/parts/runs` for half an hour while the rebuilt page it was supposed
# to be showing was never requested once.
#
# So the page carries a stamp, every response repeats it in a header, and the page compares the two
# on each poll. A stale tab now says so instead of silently lying.


def _ops_build() -> str:
    """A short id for the currently-served page assets - newest mtime across templates and CSS.

    Deliberately derived from the files rather than from a hand-maintained version constant: a
    constant that has to be remembered is a constant that goes stale, and this exists precisely to
    catch the case where something changed and nobody noticed.
    """
    newest = 0.0
    for path in [*_TEMPLATES_DIR.glob("ops/*.html"), _STATIC_DIR / "ops.css"]:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:  # a file removed under us must not take the page down
            continue
    return f"{int(newest):x}"


OPS_BUILD = _ops_build()


def _no_store(response: Response) -> Response:
    """Stop a browser serving this page (or a partial) from its own cache.

    The dashboard is a status page: a cached copy is a wrong copy. `no-store` also means a plain
    reload always fetches the rebuilt page, with no need for a hard refresh.
    """
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    response.headers["X-Ops-Build"] = OPS_BUILD
    return response


# --------------------------------------------------------------------------------------------------
# Jinja filters used across the ops templates (bytes/seconds formatting - never a fabricated number)
# --------------------------------------------------------------------------------------------------


def _fmt_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 60:
        return f"{value:.0f}s"
    minutes, seconds = divmod(value, 60)
    if minutes < 60:
        return f"{minutes:.0f}m {seconds:.0f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours:.0f}h {minutes:.0f}m"


def _fmt_pct(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1f}%"


def _fmt_usd(value: float | None) -> str:
    """``None`` means "unpriced" (no rate in ``config/model-rates.yaml``), never $0.00 - a missing
    rate must never silently read as free (see ``packages/aimemory/knowledge/telemetry.py``)."""
    return "unpriced" if value is None else f"${value:,.4f}"


def _fmt_int(value: int | None) -> str:
    return "unknown" if value is None else f"{int(value):,}"


def _fmt_ms(value: float | None) -> str:
    """A call duration in milliseconds, shown with the precision that distinguishes one LLM call
    from another (``fmt_seconds`` rounds to the whole second, which is too coarse here)."""
    if value is None:
        return "unknown"
    seconds = value / 1000.0
    return f"{seconds:.2f}s" if seconds >= 1 else f"{value:.0f}ms"


def _fmt_amsterdam_time(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo("Europe/Amsterdam")).strftime("%Y-%m-%d %H:%M:%S %Z")


templates.env.filters["fmt_bytes"] = _fmt_bytes
templates.env.filters["fmt_seconds"] = _fmt_seconds
templates.env.filters["fmt_pct"] = _fmt_pct
templates.env.filters["fmt_usd"] = _fmt_usd
templates.env.filters["fmt_int"] = _fmt_int
templates.env.filters["fmt_ms"] = _fmt_ms
templates.env.filters["fmt_amsterdam_time"] = _fmt_amsterdam_time


# --------------------------------------------------------------------------------------------------
# Section builders - one function per partial, shared by the full page and the HTMX polling routes
# --------------------------------------------------------------------------------------------------


def _health_context(runtime: Runtime) -> dict[str, object]:
    with runtime.database.session() as session:
        return {"health": queries.health_view(session, runtime.gateway)}


def _runs_context(runtime: Runtime) -> dict[str, object]:
    with runtime.database.session() as session:
        runs = queries.runs_view(session)
    return {
        "runs": runs,
        "actions": [a.value for a in RunAction],
        "tiers": [(int(t), t.name.title()) for t in Tier],
    }


def _coverage_context(runtime: Runtime) -> dict[str, object]:
    with runtime.database.session() as session:
        return {"coverage": queries.coverage_view(session)}


def _quality_charts(quality: QualityView) -> dict[str, str]:
    return {
        "validity": charts.sparkline(
            quality.schema_validity_rate, title="Schema validity", as_percent=True, fixed_domain=(0, 100)
        ),
        "failed_share": charts.sparkline(
            quality.failed_episode_share, title="Failed-episode share", as_percent=True, fixed_domain=(0, 100)
        ),
        "seconds": charts.sparkline(
            quality.median_seconds_per_episode, title="Median seconds/episode", unit="s"
        ),
        "duplicate_rate": charts.sparkline(
            quality.duplicate_entity_rate, title="Duplicate-entity rate", as_percent=True, fixed_domain=(0, 100)
        ),
        "unconfirmed_share": charts.sparkline(
            quality.unconfirmed_fact_share, title="Unconfirmed-fact share", as_percent=True, fixed_domain=(0, 100)
        ),
    }


def _quality_context(runtime: Runtime) -> dict[str, object]:
    with runtime.database.session() as session:
        quality = queries.quality_view(session)
    return {"quality": quality, "charts": _quality_charts(quality)}


def _review_context(runtime: Runtime) -> dict[str, object]:
    with runtime.database.session() as session:
        return {
            "review": queries.review_queue_view(session),
            "verdicts": [v.value for v in ReviewVerdict],
        }


def _usage_context(runtime: Runtime) -> dict[str, object]:
    with runtime.database.session() as session:
        return {"usage": queries.usage_view(session)}


def _notes_context(runtime: Runtime) -> dict[str, object]:
    with runtime.database.session() as session:
        return {"notes": queries.notes_view(session)}


def _attention_context(runtime: Runtime) -> dict[str, object]:
    with runtime.database.session() as session:
        return {"attention": queries.attention_view(session)}


# --------------------------------------------------------------------------------------------------
# Full page
# --------------------------------------------------------------------------------------------------


@router.get("/ops", response_class=HTMLResponse, summary="The offline operator dashboard")
def ops_page(request: Request) -> HTMLResponse:
    runtime: Runtime = get_runtime(request)
    context: dict[str, object] = {
        "request": request,
        "neodash_url": "http://127.0.0.1:5005",
        "ops_build": OPS_BUILD,
    }
    context.update(_health_context(runtime))
    context.update(_runs_context(runtime))
    context.update(_coverage_context(runtime))
    context.update(_quality_context(runtime))
    context.update(_usage_context(runtime))
    context.update(_review_context(runtime))
    context.update(_notes_context(runtime))
    context.update(_attention_context(runtime))
    return _no_store(templates.TemplateResponse(request, "ops/page.html", context))


# --------------------------------------------------------------------------------------------------
# HTMX polling partials (each section refreshes itself; the browser never re-fetches the whole page)
# --------------------------------------------------------------------------------------------------

_SECTION_BUILDERS = {
    "health": (_health_context, "ops/_health.html"),
    "runs": (_runs_context, "ops/_runs.html"),
    # The Runs section is split in two and only this half polls. `_runs.html` holds the <select>
    # controls and is rendered once; re-rendering it on a timer reset the operator's choice of root
    # every few seconds, which made the dropdown impossible to use. See `ops/_runs.html`.
    "runs-activity": (_runs_context, "ops/_runs_activity.html"),
    "coverage": (_coverage_context, "ops/_coverage.html"),
    "quality": (_quality_context, "ops/_quality.html"),
    "usage": (_usage_context, "ops/_usage.html"),
    "review": (_review_context, "ops/_review.html"),
    "notes": (_notes_context, "ops/_notes.html"),
    "attention": (_attention_context, "ops/_attention.html"),
}


@router.get("/ops/parts/{section}", response_class=HTMLResponse, summary="One dashboard section (HTMX poll target)")
def ops_part(section: str, request: Request) -> HTMLResponse:
    runtime: Runtime = get_runtime(request)
    builder = _SECTION_BUILDERS.get(section)
    if builder is None:
        return HTMLResponse(f"<p>unknown section: {section}</p>", status_code=404)
    build, template_name = builder
    context = {"request": request}
    context.update(build(runtime))
    return _no_store(templates.TemplateResponse(request, template_name, context))


# --------------------------------------------------------------------------------------------------
# Actions - the only two writes this router performs
# --------------------------------------------------------------------------------------------------


@router.post("/ops/runs", response_class=HTMLResponse, summary="Enqueue a run_requests row (ADR-0011)")
def create_run_request(
    request: Request,
    action: Annotated[str, Form()],
    root_id: Annotated[str, Form()] = "",
    tier: Annotated[int, Form()] = int(Tier.KNOWLEDGE),
) -> HTMLResponse:
    runtime: Runtime = get_runtime(request)
    try:
        parsed_action = RunAction(action)
        parsed_tier = Tier(tier)
    except ValueError:
        return HTMLResponse("<p class='error'>unrecognised action or tier</p>", status_code=422)

    with runtime.database.session() as session:
        created = actions.enqueue_run(
            session,
            action=parsed_action,
            root_id=(root_id or None),
            tier=parsed_tier,
            requested_by="ops-page",
        )
    logger.info("ops.run_enqueued", id=str(created.id), action=created.action.value, root=created.root_id)

    # Respond with the activity half only. Returning the whole section would replace the very form
    # that was just submitted, discarding the root and tier still selected in it.
    context = {"request": request}
    context.update(_runs_context(runtime))
    return _no_store(templates.TemplateResponse(request, "ops/_runs_activity.html", context))


@router.post("/ops/review", response_class=HTMLResponse, summary="Record one extraction_reviews verdict (ADR-0010)")
def create_review(
    request: Request,
    object_type: Annotated[str, Form()],
    object_id: Annotated[UUID, Form()],
    verdict: Annotated[str, Form()],
    sample_batch: Annotated[str, Form()] = "",
    episode_id: Annotated[str, Form()] = "",
    model_id: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    runtime: Runtime = get_runtime(request)
    try:
        parsed_type = ObjectType(object_type)
        parsed_verdict = ReviewVerdict(verdict)
    except ValueError:
        return HTMLResponse("<p class='error'>unrecognised object type or verdict</p>", status_code=422)

    with runtime.database.session() as session:
        stored = actions.record_review(
            session,
            object_type=parsed_type,
            object_id=object_id,
            verdict=parsed_verdict,
            note=(note.strip() or None),
            episode_id=(UUID(episode_id) if episode_id else None),
            model_id=(model_id or None),
            sample_batch=(sample_batch or None),
        )
    logger.info("ops.review_recorded", id=str(stored.id), object_type=object_type, verdict=verdict)

    context = {"request": request}
    context.update(_review_context(runtime))
    return _no_store(templates.TemplateResponse(request, "ops/_review.html", context))
