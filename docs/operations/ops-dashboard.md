# The Ops dashboard (`/ops`)

`memory-api` serves a built-in, offline operator page at **`http://127.0.0.1:8000/ops`** — loopback
only, no auth, server-rendered HTML with HTMX for partial refresh (ADR-0011). It replaces the CLI as
the day-to-day surface: run an update, watch quality, do the weekly review, all in a browser that never
needs the internet.

```
docker compose up -d
```

then open `http://127.0.0.1:8000/ops`.

## Offline, on purpose

Nothing on this page is fetched from a CDN. `apps/memory-api/static/htmx.min.js` (htmx.org 1.9.12) and
`apps/memory-api/static/ops.css` are committed to the repository and served by `memory-api` itself at
`/ops/static/...`. Every chart is an inline `<svg>` built in Python (`aimemory.ops.charts`) — there is
no client-side charting library, no build step, and no JavaScript beyond the vendored htmx file. Turn
off the host's network entirely and the page still works, because it never asked for anything outside
`127.0.0.1`.

## Reading the page

**Fold a section away.** Every heading is a disclosure — click it to collapse that section. The choice
is remembered in this browser (`localStorage`) and is re-applied after each background refresh, so a
section you closed stays closed instead of springing open on the next poll.

**"?" explains things in plain English.** Sections, buttons and individual numbers carry a small `?`.
It opens a floating note that says what the number means and how to read it — for example that cost
per fact is always worse than cost per document, because a document that yields nothing still costs
money. The note floats over the page rather than pushing the rows below it down, and clicking anywhere
else closes it.

**Long text is truncated.** A failed run puts a full traceback and the offending SQL in one table
cell — routinely over 2,000 characters on a single unbroken line, which used to push the six columns
that matter off the screen. You now see enough to recognise which error it is, plus a
`show all N characters` link that reveals the rest in place.

**Auto-refresh can be paused.** The header carries three controls:

| Control | What it does |
|---|---|
| **Refresh now** | Fetches every section once, immediately — works whether or not polling is paused. |
| **Auto-refresh: on / off** | Stops the timed background refresh. It turns amber when off, so a frozen page never looks like a live one. Remembered across visits. |
| **Dark mode / Light mode** | Overrides the system theme. Remembered across visits. |

Polling intervals are set by how fast the underlying number can actually change: run progress every
5s, health every 20s, coverage and attention every 30s, quality and model usage every 60s. Polling
faster than the data changes only costs queries and makes the page flicker.

**The refresh stands down while you are using the page.** A refresh replaces a section's entire DOM,
so anything open or focused inside it is destroyed — which is why an open `?` used to close itself and
a dropdown used to reset mid-selection. A slower interval does not fix that; it only makes the
interruption rarer and more surprising. A poll is therefore skipped entirely while:

- a help note, an expanded error or an expanded explanation is open,
- a dropdown, text box or note field has focus, or
- the tab is in the background.

It resumes on its own as soon as you are done. Nothing needs to be pressed.

## "Did my change actually reach the browser?"

`memory-api` bakes its templates into its image, so an edit reaches the page only after **all three**
of:

```
docker compose build memory-api
docker compose up -d memory-api
```

...and then a **reload in the browser**. Miss the last step and the open tab keeps polling the
endpoints of the version it was served, contradicting every fix you just made. That is not
hypothetical — it cost half an hour during this build: a tab polled a since-retired `/ops/parts/runs`
while the rebuilt page was never requested once.

Two things now make that visible:

- Every `/ops` response carries `Cache-Control: no-store` and an `X-Ops-Build` header, and the page
  prints the same id in its footer. A plain reload is always enough; no hard refresh needed.
- The page compares its own build id against the header on every poll. When they diverge, a banner
  appears at the top saying the tab is stale, with a link to reload.

To check which version a server is serving:

```
curl -sD - -o /dev/null http://127.0.0.1:8000/ops | grep -i x-ops-build
```

Compare it with the id in the page footer. If they differ, reload.

## What each section shows

**Health** — the same three checks as `GET /health` (PostgreSQL, Neo4j, the embedding service), plus:
the ingestion worker's own `service_stats` snapshot (Ollama model-loaded state and the worker's process
RSS — `docker stats` is not readable from inside a container, so this is the worker's own measurement,
labelled as such), disk free (the **memory-api container's own filesystem**, explicitly labelled as the
container's view, not the Windows host — `scripts/doctor` is the host-level check), and the age of the
newest file under `backups/postgres`. That last one is honestly **UNKNOWN** in the shipped compose
file: `backups/` is a host directory and no service mounts it into `memory-api` (ADR-0011 keeps every
host path and the Docker socket out of every container). Run `scripts/backup.ps1` and check the host
directory directly, or add a read-only bind mount (`./backups:/app/backups:ro`) to `memory-api` in
`docker-compose.yml` if you want the real age on the page.

**Runs** — the last 20 `run_requests` rows, and four forms that each insert exactly one row:
*Run scan* (per root, with a tier selector — 0 registry, 1 embed, 2 knowledge/LLM extraction),
*Retry failed*, *Run gold-set eval*, *Run benchmark*. The always-on ingestion worker
(`aimemory-ingest worker`, compose service `ingestion`) polls this table and executes requests one at a
time; this page and the container it runs in never call Ollama, never touch a Docker socket, and never
process a request inline inside an HTTP handler. *Run gold-set eval* and *Run benchmark* are recorded
honestly: until A12's `scripts/eval` exists, the worker marks these requests `failed` with the message
`action '<x>' is run by scripts/eval, not by the ingestion worker (ADR-0010)` — the button still does
its one job (create a real, visible request) rather than silently doing nothing.

The section is **split in two**, and the reason is worth knowing before you add anything to this page:
HTMX replaces a polled element's entire `innerHTML` on every tick, so anything you are part-way
through using inside one is destroyed. Runs used to poll whole at `every 4s`; the root `<select>` was
rebuilt under the pointer roughly every four seconds and the choice could not be held long enough to
submit. The forms now live in `ops/_runs.html`, rendered once and never swapped, while the counters
and the request table live in `ops/_runs_activity.html` and poll `hx-get="/ops/parts/runs-activity"`
every 5 seconds. Submitting a run re-renders only the activity half, so the root and tier you picked
stay picked. Review never polls at all, for the same reason — every card holds a radio group and a
note field.

**The rule: a polled partial contains no form control.** `tests/integration/test_ops_interaction.py`
enforces it for every section, so the defect cannot come back unnoticed.

**Coverage** — per project, Tier 1 (chunk+embed) and Tier 2 (LLM knowledge extraction) side by side:
indexable/embedded source counts and their percentage, and episode total/extracted/failed/pending
counts with a queue length and an ETA. The ETA is `pending episodes × median seconds/episode`, sourced
from the most recent `metrics_snapshots` row when one exists, falling back to the raw
`ingestion_jobs.extract_knowledge` durations, and reported as **unknown** (never a guess) when neither
is available yet. Coverage below 100% is never rounded up — see Attention for exactly which sources
make up the shortfall and why each one is a settled outcome, not a stuck job.

**Quality** — the ADR-0010 metrics as inline SVG trend lines (schema validity rate, failed-episode
share, median seconds/episode, duplicate-entity rate, unconfirmed-fact share) read from
`metrics_snapshots`; the acceptance rate from `extraction_reviews`; the model-upgrade rule (acceptance
< 70% over two consecutive review batches, or validity < 85% over two runs) evaluated live and shown as
a notice banner when it fires; and the list of benchmark reports found under `reports/benchmark-*.md`.
Rate/share charts are drawn on a fixed 0–100% axis on purpose (a metric that moved from 96% to 94% must
not fill the whole chart height and look like a collapse); `median seconds/episode` gets a data-driven,
zero-based axis because it has no natural ceiling. Fewer than two data points renders as an honest "not
enough data yet" message, never a single dot pretending to be a trend.

**Review queue** — up to 20 recent artifacts and facts that have no `extraction_reviews` row yet,
newest first, each with its evidence quote (artifacts) or natural-language statement (facts — they have
no `evidence_quote` column) and its citation (`[source_uri#heading @hash8]`, built from the same
`Provenance.citation()` every other surface uses). Never raw file content. `accept | wrong | partial` +
an optional note POSTs to `/ops/review`, which inserts one `extraction_reviews` row and re-renders the
queue — the reviewed item drops out immediately.

**Attention** — things a person can act on, not a restatement of the counters above:
unextracted-episode count (usually the single most useful number on the page), the named
INDEX_CONTENT/MIRROR sources that produced no chunk (a settled outcome — extractor/duplicate/empty
reasons, not pending retries), sources that fell back to `CATALOG_ONLY` at *runtime* rather than by
`policies.yaml` configuration (extractor failure, duplicate hash, secret detected), the
secret-suspected source(s) by name, deleted sources (knowledge kept per ADR-0005, never erased),
unconfirmed facts, artifacts with no evidence quote, refused MCP writes from `mcp_audit_log` (every
refusal is logged, none of them wrote anything), and the same model-upgrade notice as Quality when it
fires.

**Model usage** — cost, volume, speed and reliability from `llm_calls` (migration 0004, one row per
LLM call, not per document — a document makes 2-3 calls: entity extraction, relationship extraction,
and an optional retry). Cost and volume: total calls, input/output tokens and total cost, the same
broken down per model, the count of **unpriced** calls (`cost_usd IS NULL` — no rate in
`config/model-rates.yaml`, shown as a count so a missing rate is visible rather than silently read as
free), and cost per document / cost per extracted fact. Those last two are computed only over the
episodes that actually have a recorded call and the facts extracted from exactly those episodes —
never against the whole historical corpus, most of which predates this table and would otherwise make
the ratio read artificially low. Speed: median/p90/p95 `duration_ms` split by `purpose` (the
relationship call re-sends the document body and behaves very differently from the entity call, so a
blended number would hide that) and output throughput (completion tokens/second). Reliability: error
rate (`ok = false`) and retry rate (`attempts > 1` — the provider had to retry invalid output). The
page states plainly that **time-to-first-token is not measurable here**: extraction calls the model
for non-streaming structured output, so the response only exists once it is complete: total latency
and output throughput are the meaningful equivalents for this workload. A last-20 recent-calls table
sits at the bottom for spotting one pathological document. The section renders correctly — zeroes and
"no calls recorded yet", not a crash — when `llm_calls` is empty, which it legitimately is until an
extraction runs after the recording hook (`packages/aimemory/knowledge/telemetry.py`, owned by A00) was
wired.

A link to NeoDash (`http://127.0.0.1:5005`) sits in the header — NeoDash remains the graph-exploration
tool; this page does not try to replace it.

## Theme

The page respects `prefers-color-scheme` by default and offers a toggle (top-right of the header,
`#theme-toggle`) that forces light or dark regardless of the OS setting; the choice is remembered in
`localStorage` (`ops-theme`) and re-applied on the next load via a small inline script in
`apps/memory-api/templates/ops/base.html`, run before first paint so there is no flash of the wrong
theme. Both themes are defined entirely in `apps/memory-api/static/ops.css` as CSS custom properties
(`--fg`, `--bg`, `--muted`, `--border`, `--panel`, `--ok`, `--warn`, `--notice`, `--info`, `--link`) —
nothing is fetched, computed, or duplicated in HTML. Every status colour and table rule uses one of
these variables, so badges, borders and hover states stay readable in both themes (MEASURED: WCAG
relative-luminance contrast ≥ 5:1 for every foreground/background pair actually used on the page, in
both themes — well above the 4.5:1 AA threshold for body text).

## What it never does

* Never calls Docker or reads a Docker socket — no container the Ops page talks to has one.
* Never runs an ingest, calls Ollama, or does LLM work inside an HTTP request. Every action button
  inserts one `run_requests` row; the worker is the only executor.
* Never returns raw file bytes — only citations, evidence quotes/statements, and metrics.
* Never fetches anything off `127.0.0.1`.
* No auth in V0.1 (ADR-0011) — this is a loopback-only page for the one operator of this machine.

## Layout

| Path | Owner | Contents |
|---|---|---|
| `packages/aimemory/ops/queries.py` | A16 | Read-only SQL behind every section |
| `packages/aimemory/ops/viewmodels.py` | A16 | Plain dataclasses the templates render |
| `packages/aimemory/ops/charts.py` | A16 | Inline SVG trend charts, no JS |
| `packages/aimemory/ops/actions.py` | A16 | The two writes: enqueue a run, record a review verdict |
| `apps/memory-api/routes/ops.py` | A16 | The FastAPI router — `/ops`, `/ops/parts/{section}`, `/ops/runs`, `/ops/review`, `/ops/static/*` |
| `apps/memory-api/templates/ops/*.html` | A16 | Jinja2 templates (one full page + seven partials) |
| `apps/memory-api/static/{htmx.min.js,ops.css}` | A16 | Vendored HTMX + plain CSS (themed, see Theme above) |
| `packages/aimemory/sources/worker.py` | A07a | The always-on `aimemory-ingest worker` that executes `run_requests` |
| `infra/postgres/alembic/versions/0002_ops_eval.py` | A04 | `run_requests`, `metrics_snapshots`, `extraction_reviews`, `service_stats` |
| `infra/postgres/alembic/versions/0004_llm_calls.py` | A00 | `llm_calls` — one row per LLM call, `cost_usd` generated from `config/model-rates.yaml` |
| `packages/aimemory/knowledge/telemetry.py` | A00 | Records every LLM call into `llm_calls`; never breaks extraction on a bookkeeping failure |

## Troubleshooting

* **Health shows the worker as "stale"** — no `service_stats` row from `service='ingestion'` in the
  last 5 minutes. Check `docker compose ps ingestion` and `docker compose logs ingestion`; the always-on
  worker should read `Up`. The CLI path (`docker compose exec ingestion aimemory-ingest run ...`) still
  works even if the worker container is down — only this page's buttons depend on it.
* **A run stays `queued`** — the worker processes one request at a time and polls every
  `INGEST_WORKER_POLL_SECONDS` (default 5s, `config`/`.env`); a long-running scan on another root will
  delay it, by design (ADR-0006: serial, so the frugal-RAM constraint holds).
* **Quality is empty** — `metrics_snapshots` only gets a row after a `scan` run_request completes (the
  worker writes it, see `write_metrics_snapshot` in `packages/aimemory/sources/worker.py`). Click *Run
  scan* and wait for it to finish.
* **Model usage is empty** — `llm_calls` only gets a row once an extraction call actually runs after
  the recording hook (`packages/aimemory/knowledge/telemetry.py`) was wired; a page with no rows yet is
  the correct, honest state, not a bug. **Unpriced** shows a non-zero count instead of `$0.00` when a
  model is missing from `config/model-rates.yaml` — add the model there (input/output price per
  million tokens) and its *future* calls will price; past rows keep the rate that was in force when
  they ran, by design, and never re-price themselves.
