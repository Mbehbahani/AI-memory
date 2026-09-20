"""Plain dataclasses rendered by ``apps/memory-api/templates/ops/*.html``.

No pydantic here on purpose: nothing in this module crosses a process boundary (the templates render
these in-process), so the strict validation :class:`~aimemory.domain.base.DomainModel` buys elsewhere
would only add ceremony. Every field is either MEASURED (queried straight from Postgres) or carries an
explicit ``UNKNOWN``/``None`` when the number cannot be produced honestly - never a fabricated default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from ..common.time import utc_now

__all__ = [
    "AttentionItem",
    "AttentionView",
    "BackupStatus",
    "ComponentCheck",
    "CoverageProjectRow",
    "CoverageView",
    "DiskStatus",
    "HealthView",
    "ModelUsageRow",
    "PurposeLatencyRow",
    "QualityView",
    "RecentCallRow",
    "ReviewCandidate",
    "ReviewQueueView",
    "RootOption",
    "RunRequestRow",
    "RunsView",
    "SnapshotPoint",
    "SourceRootOption",
    "UsageView",
    "WorkerStatus",
]


# --------------------------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------------------------


@dataclass
class ComponentCheck:
    """One ``gateway.health()`` dependency check, as shown on the page."""

    name: str
    ok: bool
    detail: str = ""
    latency_ms: int = 0


@dataclass
class WorkerStatus:
    """The ingestion worker's own :class:`~aimemory.domain.models.ServiceStat` snapshot.

    ``docker stats`` is unreachable from inside a container (ADR-0011), so this is the worker's own
    process RSS and Ollama's ``/api/ps`` model-loaded flag - never a host-level number.
    """

    seen: bool
    at: datetime | None = None
    model_loaded: bool | None = None
    model_name: str | None = None
    process_rss_bytes: int | None = None
    llm_provider: str | None = None
    stale: bool = False
    stale_after_seconds: int = 0


@dataclass
class BackupStatus:
    """Age of the newest ``backups/postgres`` archive, honestly labelled.

    ``backups/`` is a host directory outside the memory-api container's bind mounts in V0.1 (ADR-0011
    does not put the Docker socket, or any host path, in this container) - so unless a future compose
    change mounts it read-only, this is reported as UNKNOWN rather than guessed at.
    """

    mounted: bool
    newest_at: datetime | None = None
    newest_name: str | None = None
    age_seconds: float | None = None
    note: str = ""


@dataclass
class DiskStatus:
    """``shutil.disk_usage`` on the memory-api container's own filesystem.

    Labelled explicitly: this is the *container's* view of its own writable layer, not the Windows
    host's free space (plan section AG says the container view is what is available; host RAM/disk is
    ``scripts/doctor``'s job).
    """

    total_bytes: int
    used_bytes: int
    free_bytes: int
    path: str


@dataclass
class HealthView:
    status: str
    writes_enabled: bool
    checks: list[ComponentCheck]
    worker: WorkerStatus
    backup: BackupStatus
    disk: DiskStatus
    generated_at: datetime


# --------------------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------------------


@dataclass
class RunRequestRow:
    id: UUID
    action: str
    root_id: str | None
    tier: int
    status: str
    progress_pct: int
    message: str | None
    requested_by: str
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None


@dataclass
class SourceRootOption:
    root_id: str
    label: str


@dataclass
class RootOption:
    """Alias kept for template readability; identical shape to :class:`SourceRootOption`."""

    root_id: str
    label: str


@dataclass
class FreshnessView:
    """"Is the memory out of date?" - the answer to the one question Coverage cannot answer.

    Coverage counts files the system already knows about, so a file edited after its last scan still
    reads as fully embedded. Nothing else on this page notices that the copy in the database no longer
    matches the copy on disk, and the only visible symptom is a confidently wrong answer.

    Every number here was MEASURED by the ingestion worker, which is the only container with the
    source folders mounted (read-only) - see ``aimemory.sources.freshness``. The Ops page performs no
    filesystem work of its own, so this is always *as of* ``checked_at``, never live. That timestamp
    is shown for exactly that reason: a stale staleness indicator would be its own joke.
    """

    checked_at: datetime | None = None
    files_on_disk: int = 0
    unchanged: int = 0
    changed: int = 0
    added: int = 0
    removed: int = 0
    #: Files whose timestamp moved but whose bytes did not (`touch`, a sync client, a git checkout).
    #: Counted apart from `changed` on purpose: they are not work, and folding them in would inflate
    #: the number the operator is meant to act on until they learn to ignore it.
    touched_not_changed: int = 0
    hashed: int = 0
    duration_ms: int = 0
    roots_checked: list[str] = field(default_factory=list)
    changed_sample: list[str] = field(default_factory=list)
    added_sample: list[str] = field(default_factory=list)
    removed_sample: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: False when the worker has never recorded a check - a fresh stack, or a worker that is down.
    #: Rendered as "not checked yet", never as zero: "0 files changed" from a check that never ran is
    #: the most dangerous sentence this page could print.
    has_check: bool = False

    @property
    def stale_files(self) -> int:
        """What a scan would actually process now."""
        return self.changed + self.added + self.removed

    @property
    def is_stale(self) -> bool:
        return self.stale_files > 0

    @property
    def age_seconds(self) -> float | None:
        if self.checked_at is None:
            return None
        return max(0.0, (utc_now() - self.checked_at).total_seconds())

    @property
    def check_is_old(self) -> bool:
        """The worker checks every 2 minutes; 10 suggests it is not running."""
        age = self.age_seconds
        return age is not None and age > 600

    @property
    def headline(self) -> str:
        """One sentence, plain English, safe to print anywhere."""
        if not self.has_check:
            return "Not checked yet - the ingestion worker records this; it may be starting or down."
        if not self.is_stale:
            return "Up to date - every file on disk matches the copy in memory."
        parts = []
        if self.changed:
            parts.append(f"{self.changed} changed")
        if self.added:
            parts.append(f"{self.added} new")
        if self.removed:
            parts.append(f"{self.removed} removed")
        return f"{' · '.join(parts)} since the last scan - run a scan to pick them up."


@dataclass
class RunsView:
    recent: list[RunRequestRow]
    roots: list[SourceRootOption]
    pending_episodes: int
    queued_or_running: int
    freshness: FreshnessView = field(default_factory=lambda: FreshnessView())


# --------------------------------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------------------------------


@dataclass
class CoverageProjectRow:
    project_id: str
    sources_indexable: int
    sources_embedded: int
    episodes_total: int
    episodes_extracted: int
    episodes_failed: int

    @property
    def episodes_pending(self) -> int:
        return max(0, self.episodes_total - self.episodes_extracted - self.episodes_failed)

    @property
    def embed_coverage_pct(self) -> float | None:
        if not self.sources_indexable:
            return None
        return round(100.0 * self.sources_embedded / self.sources_indexable, 1)

    @property
    def extraction_coverage_pct(self) -> float | None:
        if not self.episodes_total:
            return None
        return round(100.0 * self.episodes_extracted / self.episodes_total, 1)

    def eta_seconds(self, median_seconds_per_episode: float | None) -> float | None:
        if median_seconds_per_episode is None:
            return None
        return round(self.episodes_pending * median_seconds_per_episode, 1)


@dataclass
class CoverageView:
    rows: list[CoverageProjectRow]
    median_seconds_per_episode: float | None
    median_seconds_source: str
    totals: CoverageProjectRow


# --------------------------------------------------------------------------------------------------
# Quality
# --------------------------------------------------------------------------------------------------


@dataclass
class SnapshotPoint:
    at: datetime
    value: float | None


@dataclass
class QualityView:
    """ADR-0010 metrics: the trend charts, the acceptance rate, and the benchmark reports on disk."""

    has_snapshots: bool
    latest_at: datetime | None
    schema_validity_rate: list[SnapshotPoint]
    failed_episode_share: list[SnapshotPoint]
    median_seconds_per_episode: list[SnapshotPoint]
    duplicate_entity_rate: list[SnapshotPoint]
    unconfirmed_fact_share: list[SnapshotPoint]
    review_total: int
    review_accept: int
    review_wrong: int
    review_partial: int
    acceptance_rate: float | None
    benchmark_reports: list[str]
    upgrade_notice: str | None


# --------------------------------------------------------------------------------------------------
# Review queue
# --------------------------------------------------------------------------------------------------


@dataclass
class ReviewCandidate:
    object_type: str
    object_id: UUID
    headline: str
    evidence: str | None
    citation: str
    project_id: str | None
    observed_at: datetime | None
    episode_id: UUID | None
    model_id: str | None


@dataclass
class ReviewQueueView:
    sample: list[ReviewCandidate]
    sample_batch: str
    already_reviewed_today: int


# --------------------------------------------------------------------------------------------------
# Model usage (llm_calls, migration 0004 - one row per LLM call, not per document)
# --------------------------------------------------------------------------------------------------


@dataclass
class ModelUsageRow:
    """Totals for one ``(provider, model_id)`` pair across all recorded calls."""

    provider: str
    model_id: str
    calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    unpriced_calls: int


@dataclass
class PurposeLatencyRow:
    """Latency/throughput split by ``purpose`` - the relationship call re-sends the document body
    and behaves very differently from the entity call, so a single blended number hides the
    interesting part."""

    purpose: str
    count: int
    median_ms: float | None
    p90_ms: float | None
    p95_ms: float | None
    tokens_per_second: float | None


@dataclass
class RecentCallRow:
    """One row of the last-N-calls table - enough to spot a single pathological document."""

    at: datetime
    provider: str
    model_id: str
    purpose: str
    prompt_tokens: int | None
    completion_tokens: int | None
    duration_ms: int | None
    cost_usd: float | None
    ok: bool
    attempts: int


@dataclass
class UsageView:
    """Cost, volume, speed and reliability for every LLM call recorded since migration 0004.

    ``cost_per_document``/``cost_per_fact`` are computed only over the episodes that actually have a
    recorded call (``documents_priced``) and the facts extracted from exactly those episodes
    (``facts_priced``) - never against the whole historical corpus, most of which predates this
    table and would otherwise make cost-per-fact read artificially low.
    """

    has_calls: bool
    total_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_usd: float | None
    unpriced_calls: int
    by_model: list[ModelUsageRow]
    documents_priced: int
    facts_priced: int
    cost_per_document: float | None
    cost_per_fact: float | None
    by_purpose: list[PurposeLatencyRow]
    error_rate_pct: float | None
    retry_rate_pct: float | None
    recent: list[RecentCallRow]
    ttft_note: str


# --------------------------------------------------------------------------------------------------
# Attention
# --------------------------------------------------------------------------------------------------


@dataclass
class NoteRow:
    """One piece of knowledge that did not come from a file the system discovered on its own.

    Sessions, decisions and hand-written notes are the only knowledge nobody can find by browsing a
    folder, so they are the only knowledge that needs a list.
    """

    at: datetime
    kind: str               #: "mcp" | "manual" | "session note"
    title: str
    project_id: str | None
    status: str
    facts: int
    artifacts: int
    searchable: bool        #: False for an MCP/manual write: chunked text exists only for files
    source_uri: str | None


@dataclass
class NotesView:
    """The "what have I saved?" panel.

    Exists because nothing else answers it. `/v1/sources` lists files; `memory_search` needs
    embeddings, and an MCP write produces facts without chunks, so its sentences are unfindable by
    meaning-search. Before this, twenty saved sessions would have been twenty rows only SQL could see.
    """

    rows: list[NoteRow] = field(default_factory=list)
    total: int = 0
    unsearchable: int = 0   #: how many hold knowledge no text search can reach

    @property
    def has_any(self) -> bool:
        return bool(self.rows)


@dataclass
class AttentionItem:
    """One actionable line. ``severity`` drives the CSS class, never JS."""

    severity: str  # "notice" | "warning" | "info"
    title: str
    detail: str
    items: list[str] = field(default_factory=list)


@dataclass
class AttentionView:
    items: list[AttentionItem]
