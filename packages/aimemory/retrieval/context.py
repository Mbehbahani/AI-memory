"""Stage 8: context assembly (``retrieval.md`` §8, plan section P step 6).

Pure functions over already-retrieved data: this module never touches PostgreSQL or Neo4j, so the
budgeter, the ordering, the truncation policy and the track labelling are unit-testable without a
database, and the Gateway stays the only place that queries.

The rules that are *not* negotiable here:

* blocks are emitted in ``context.block_order`` and the whole thing is capped by
  ``context.token_budget`` (6000);
* only ``evidence_chunks`` may be partially included - every other block is all-or-nothing, because
  a half-quoted decision is worse than an omitted one (``retrieval.md`` deviation 4). What was cut is
  named in :attr:`AssembledContext.dropped_blocks`;
* every block carries its own citations, and ``flags`` marks ``unconfirmed`` / ``source-deleted`` /
  ``low-trust`` inline, so a reader sees the caveat next to the claim;
* **business and research results are labelled, never merged silently** (plan section Q). Where a
  block mixes tracks, each track gets its own labelled sub-section and
  :func:`track_mix_warning` returns the notice the Gateway puts in ``SearchResult.warnings``.

Token counting is ``ceil(len(text) / 4)`` characters-per-token (``retrieval.md`` deviation 5): no
tokenizer exists in the Gateway container, the estimate is consistent between assembly and
budgeting, and it is labelled **ESTIMATED** wherever it is reported. It is a budget guard, never a
billing number.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from ..common.time import isoformat_utc
from ..domain.enums import FactStatus, SourceStatus, Trust
from ..domain.provenance import Provenance
from ..domain.retrieval import (
    AssembledContext,
    ContextBlock,
    RelatedEntity,
    RetrievalConfig,
    ScoredHit,
)
from .temporal import FactRow

__all__ = [
    "BLOCK_ORDER",
    "TRACK_MIX_WARNING",
    "ContextInputs",
    "DecisionRow",
    "ProjectSummary",
    "assemble_context",
    "estimate_tokens",
    "track_mix_warning",
]

#: Fallback when ``config/retrieval.yaml`` is missing ``context.block_order``.
BLOCK_ORDER: tuple[str, ...] = (
    "project_summary",
    "current_facts",
    "decisions",
    "evidence_chunks",
    "related_entities",
)

#: Plan section Q: business and research results are labelled, never merged silently.
TRACK_MIX_WARNING = (
    "results span more than one track; they are labelled by track and not merged"
)

_UNKNOWN_TRACK = "unknown"


def estimate_tokens(text: str) -> int:
    """ESTIMATED token count: ``ceil(len(text) / 4)`` (``retrieval.md`` deviation 5)."""
    return math.ceil(len(text) / 4) if text else 0


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    """The ``project_summary`` block's input: one registry row plus its honest coverage note."""

    project_id: str
    name: str
    track: str
    status: str
    summary: str | None = None
    coverage_note: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionRow:
    """A current decision artifact for the ``decisions`` block, with its supersession line."""

    id: UUID
    title: str
    body: str
    status: str
    project_id: str | None = None
    valid_from: datetime | None = None
    supersedes_title: str | None = None
    supersedes_id: UUID | None = None
    citation: str = ""
    provenance: Provenance | None = None
    source_status: str = SourceStatus.ACTIVE.value


@dataclass
class ContextInputs:
    """Everything the assembler is allowed to see. The Gateway fills it in one read transaction."""

    projects: list[ProjectSummary] = field(default_factory=list)
    facts: list[FactRow] = field(default_factory=list)
    decisions: list[DecisionRow] = field(default_factory=list)
    hits: list[ScoredHit] = field(default_factory=list)
    related: list[RelatedEntity] = field(default_factory=list)
    tracks: dict[str, str] = field(default_factory=dict)
    hit_flags: dict[UUID, list[str]] = field(
        default_factory=dict,
        metadata={"why": "source-deleted / low-trust live on HitMetadata, not on the frozen ScoredHit"},
    )

    def track_of(self, project_id: str | None) -> str:
        """``business`` / ``research`` / ``unknown`` - never a guess, only the registry's answer."""
        if not project_id:
            return _UNKNOWN_TRACK
        return self.tracks.get(project_id, _UNKNOWN_TRACK)


def track_mix_warning(inputs: ContextInputs) -> str | None:
    """:data:`TRACK_MIX_WARNING` when the answer spans more than one *known* track."""
    tracks = {inputs.track_of(hit.project_id) for hit in inputs.hits}
    tracks |= {inputs.track_of(decision.project_id) for decision in inputs.decisions}
    known = {track for track in tracks if track != _UNKNOWN_TRACK}
    return TRACK_MIX_WARNING if len(known) > 1 else None


def _grouped_by_track[T](
    items: Sequence[T], project_ids: Sequence[str | None], inputs: ContextInputs
) -> list[tuple[str, list[T]]]:
    """Stable grouping ``[(track, items)]`` in first-appearance order - the labelling contract."""
    order: list[str] = []
    groups: dict[str, list[T]] = {}
    for item, project_id in zip(items, project_ids, strict=True):
        track = inputs.track_of(project_id)
        if track not in groups:
            groups[track] = []
            order.append(track)
        groups[track].append(item)
    return [(track, groups[track]) for track in order]


def _track_heading(track: str) -> str:
    return f"[{track} track]"


def _project_summary_block(inputs: ContextInputs) -> ContextBlock | None:
    if not inputs.projects:
        return None
    lines: list[str] = []
    for project in inputs.projects:
        lines.append(
            f"{project.name} ({project.project_id}) - track: {project.track}, "
            f"status: {project.status}"
        )
        if project.summary:
            lines.append(f"  {project.summary}")
        if project.coverage_note:
            lines.append(f"  coverage: {project.coverage_note}")
    body = "\n".join(lines)
    return ContextBlock(
        kind="project_summary",
        title="Projects",
        text=body,
        tokens=estimate_tokens(body),
        flags=sorted({f"track:{p.track}" for p in inputs.projects}),
    )


def _fact_line(fact: FactRow) -> str:
    marker = " [unconfirmed]" if fact.is_unconfirmed else ""
    citation = f" {fact.provenance.citation()}" if fact.provenance.source_uri else ""
    return f"- {fact.statement}{marker}{citation}"


def _current_facts_block(inputs: ContextInputs) -> ContextBlock | None:
    if not inputs.facts:
        return None
    groups = _grouped_by_track(
        list(inputs.facts), [fact.project_id for fact in inputs.facts], inputs
    )
    lines: list[str] = []
    citations: list[str] = []
    flags: set[str] = set()
    for track, facts in groups:
        if len(groups) > 1:
            lines.append(_track_heading(track))
        for fact in facts:
            lines.append(_fact_line(fact))
            if fact.provenance.source_uri:
                citations.append(fact.provenance.citation())
            if fact.is_unconfirmed:
                flags.add(FactStatus.UNCONFIRMED.value)
        flags.add(f"track:{track}")
    body = "\n".join(lines)
    return ContextBlock(
        kind="current_facts",
        title="Current facts",
        text=body,
        tokens=estimate_tokens(body),
        citations=citations,
        object_ids=[fact.id for fact in inputs.facts],
        flags=sorted(flags),
    )


def _decision_lines(decision: DecisionRow) -> list[str]:
    head = f"- {decision.title} [{decision.status}]"
    if decision.valid_from:
        head += f" (since {isoformat_utc(decision.valid_from)})"
    if decision.citation:
        head += f" {decision.citation}"
    lines = [head]
    body = decision.body.strip()
    if body:
        lines.append(f"  {body}")
    if decision.supersedes_title or decision.supersedes_id:
        target = decision.supersedes_title or str(decision.supersedes_id)
        lines.append(f"  supersedes: {target}")
    return lines


def _decisions_block(inputs: ContextInputs) -> ContextBlock | None:
    if not inputs.decisions:
        return None
    groups = _grouped_by_track(
        list(inputs.decisions), [d.project_id for d in inputs.decisions], inputs
    )
    lines: list[str] = []
    citations: list[str] = []
    flags: set[str] = set()
    for track, decisions in groups:
        if len(groups) > 1:
            lines.append(_track_heading(track))
        for decision in decisions:
            lines.extend(_decision_lines(decision))
            if decision.citation:
                citations.append(decision.citation)
            if decision.status == FactStatus.UNCONFIRMED.value:
                flags.add(FactStatus.UNCONFIRMED.value)
            if decision.source_status == SourceStatus.DELETED.value:
                flags.add("source-deleted")
        flags.add(f"track:{track}")
    body = "\n".join(lines)
    return ContextBlock(
        kind="decisions",
        title="Decisions",
        text=body,
        tokens=estimate_tokens(body),
        citations=citations,
        object_ids=[decision.id for decision in inputs.decisions],
        flags=sorted(flags),
    )


def _hit_flags(hit: ScoredHit, inputs: ContextInputs) -> list[str]:
    """``unconfirmed`` / ``low-trust`` / ``source-deleted`` for one hit, from what it carries."""
    flags = set(inputs.hit_flags.get(hit.object_id, ()))
    if hit.status == FactStatus.UNCONFIRMED.value:
        flags.add(FactStatus.UNCONFIRMED.value)
    if "low_trust" in hit.boosts:
        flags.add(f"{Trust.LOW.value}-trust")
    return sorted(flags)


def _hit_lines(hit: ScoredHit, inputs: ContextInputs) -> list[str]:
    flags = _hit_flags(hit, inputs)
    marker = f" [{', '.join(flags)}]" if flags else ""
    title = hit.title or hit.citation or str(hit.object_id)
    lines = [f"[{hit.rank}] {title}{marker} {hit.citation}".rstrip()]
    text = hit.text.strip()
    if text:
        lines.append(text)
    return lines


def _evidence_block(inputs: ContextInputs) -> ContextBlock | None:
    if not inputs.hits:
        return None
    groups = _grouped_by_track(list(inputs.hits), [hit.project_id for hit in inputs.hits], inputs)
    lines: list[str] = []
    citations: list[str] = []
    flags: set[str] = set()
    for track, hits in groups:
        if len(groups) > 1:
            lines.append(_track_heading(track))
        for hit in hits:
            lines.extend(_hit_lines(hit, inputs))
            if hit.citation:
                citations.append(hit.citation)
            flags.update(_hit_flags(hit, inputs))
        flags.add(f"track:{track}")
    body = "\n".join(lines)
    return ContextBlock(
        kind="evidence_chunks",
        title="Evidence",
        text=body,
        tokens=estimate_tokens(body),
        citations=citations,
        object_ids=[hit.object_id for hit in inputs.hits],
        flags=sorted(flags),
    )


def _related_block(inputs: ContextInputs) -> ContextBlock | None:
    if not inputs.related:
        return None
    lines = [
        f"- {item.name} ({item.type.value}) --{item.predicate}--> "
        f"{'outgoing' if item.direction == 'out' else 'incoming'}"
        for item in inputs.related
    ]
    body = "\n".join(lines)
    return ContextBlock(
        kind="related_entities",
        title="Related entities",
        text=body,
        tokens=estimate_tokens(body),
        object_ids=[item.entity_id for item in inputs.related],
        flags=sorted({f"track:{inputs.track_of(item.project_id)}" for item in inputs.related}),
    )


_BUILDERS = {
    "project_summary": _project_summary_block,
    "current_facts": _current_facts_block,
    "decisions": _decisions_block,
    "evidence_chunks": _evidence_block,
    "related_entities": _related_block,
}

#: The only block that may be cut mid-way (``retrieval.md`` deviation 4).
TRUNCATABLE = "evidence_chunks"


def _truncate(block: ContextBlock, budget: int) -> ContextBlock | None:
    """Cut ``evidence_chunks`` to fit. Whole lines only, so a quote is never severed mid-sentence."""
    if budget <= 0:
        return None
    kept: list[str] = []
    used = 0
    for line in block.text.splitlines():
        cost = estimate_tokens(line + "\n")
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    if not kept:
        return None
    text = "\n".join(kept) + "\n[truncated: token budget reached]"
    tokens = estimate_tokens(text)
    if tokens > budget:  # the marker itself must fit
        text = "\n".join(kept)
        tokens = estimate_tokens(text)
    citations = [c for c in block.citations if c in text]
    return ContextBlock(
        kind=block.kind,
        title=block.title,
        text=text,
        tokens=tokens,
        citations=citations,
        object_ids=list(block.object_ids),
        flags=sorted({*block.flags, "truncated"}),
    )


def assemble_context(
    inputs: ContextInputs,
    *,
    config: RetrievalConfig | None = None,
    block_order: Sequence[str] | None = None,
    token_budget: int | None = None,
) -> AssembledContext:
    """Build the ordered, budgeted context (``retrieval.md`` §8 algorithm, verbatim).

    An unknown block name in ``context.block_order`` is skipped and recorded in ``dropped_blocks``
    rather than raising: a config typo degrades the context, it does not break search.
    """
    order = list(block_order or (config.block_order if config else []) or BLOCK_ORDER)
    budget = int(
        token_budget if token_budget is not None else (config.token_budget if config else 6000)
    )
    remaining = budget
    blocks: list[ContextBlock] = []
    dropped: list[str] = []
    truncated = False

    for kind in order:
        builder = _BUILDERS.get(kind)
        if builder is None:
            dropped.append(kind)
            truncated = True
            continue
        block = builder(inputs)
        if block is None:
            continue
        if block.tokens <= remaining:
            blocks.append(block)
            remaining -= block.tokens
            continue
        if kind == TRUNCATABLE:
            cut = _truncate(block, remaining)
            if cut is not None:
                blocks.append(cut)
                remaining -= cut.tokens
            else:
                dropped.append(kind)
            truncated = True
            continue
        dropped.append(kind)
        truncated = True

    citations: list[str] = []
    for block in blocks:
        for citation in block.citations:
            if citation not in citations:
                citations.append(citation)

    return AssembledContext(
        blocks=blocks,
        token_budget=budget,
        tokens_used=budget - remaining,
        truncated=truncated,
        dropped_blocks=dropped,
        citations=citations,
    )
