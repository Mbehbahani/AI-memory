"""The ADR-0005 temporal rules, implemented exactly as ``docs/architecture/temporal.md`` §4-§6
specifies them (A08, P8-T01).

One sentence this module exists to protect: **the memory never forgets; it only changes what is
currently true.** Nothing here deletes a row. ``close`` sets ``valid_to``; rule 3 sets a *status*;
rule 4 sets a *flag*.

Entry points, in the order the pipeline calls them:

``apply_fact``       one extracted fact -> one of six outcomes (explicit, first_value, reconfirmed,
                     backdated, superseded, added). This is the whole of ADR-0005 rules 1, 2 and 5.
``close``            the port-level ``KnowledgeEngine.invalidate``. Idempotent, because the ingestion
                     state machine may replay the ``temporal`` stage after a crash (plan section L).
``reconcile_version`` rule 3: facts of the previous version that the new version did not re-state
                     become ``unconfirmed`` - still current, ranked down, flagged. Never closed.
``mark_source_deleted`` rule 4: flag the source and its derived rows; erase nothing.

Ordering against the database backstop
--------------------------------------
``uq_facts_functional_current`` is a partial unique index on ``(subject_entity_id, predicate) WHERE
valid_to IS NULL AND predicate IN <the six functional predicates>``. The functional branch therefore
**closes the open fact before inserting the new one, in the same transaction**. That ordering is not
a defensive nicety - reverse it and PostgreSQL rejects the insert. A violation of that index is a bug
in this module's ordering and is never to be worked around by touching the index (ADR-0005, A04).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from ...common.errors import AiMemoryError
from ...common.logging import get_logger
from ...common.time import ensure_utc, is_valid_at, utc_now
from ...domain.enums import FactStatus, SourceStatus
from ...domain.models import Fact
from ...ontology import Ontology, load_ontology
from .store import FactStore, GraphSink, fact_fingerprint

__all__ = [
    "TemporalAction",
    "TemporalOutcome",
    "TemporalRuleError",
    "apply_fact",
    "apply_facts",
    "close",
    "is_valid_at",
    "mark_source_deleted",
    "reconcile_version",
]

logger = get_logger(__name__)


class TemporalRuleError(AiMemoryError):
    """An ADR-0005 invariant was violated by the caller (e.g. closing a fact before it opened)."""

    code = "temporal_rule_error"
    http_status = 409


class TemporalAction(StrEnum):
    """Which branch of ``apply_fact`` ran. Recorded so a run can be explained, not just counted."""

    EXPLICIT = "explicit"
    FIRST_VALUE = "first_value"
    RECONFIRMED = "reconfirmed"
    BACKDATED = "backdated"
    SUPERSEDED = "superseded"
    ADDED = "added"


@dataclass(slots=True)
class TemporalOutcome:
    """What ``apply_fact`` did: the stored fact, what it closed, and anything worth reporting."""

    action: TemporalAction
    fact: Fact
    closed_fact_id: UUID | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def superseded(self) -> bool:
        return self.closed_fact_id is not None


# --------------------------------------------------------------------------------------------------
# close() - temporal.md §4, and the KnowledgeEngine.invalidate port entry point
# --------------------------------------------------------------------------------------------------


def close(
    fact: Fact | UUID,
    *,
    at: datetime,
    by_episode: UUID | None,
    store: FactStore,
    reason: str = "functional",
    graph: GraphSink | None = None,
) -> Fact | None:
    """Close ``fact`` at ``at``: ``valid_to``, ``status=historical``, who and when.

    Idempotent - re-running an episode closes the same fact at the same instant and changes nothing,
    which is exactly why the ingestion state machine may replay the ``temporal`` stage.

    Raises :class:`TemporalRuleError` when ``at`` precedes ``valid_from``: a fact can never stop being
    true before it started being true, and silently clamping would corrupt the timeline instead of
    reporting the bug that produced it.
    """
    resolved = fact if isinstance(fact, Fact) else store.get(fact)
    if resolved is None:
        return None
    moment = ensure_utc(at)
    if moment < ensure_utc(resolved.valid_from):
        raise TemporalRuleError(
            "Refusing to close a fact before it became valid.",
            detail=f"fact={resolved.id} valid_from={resolved.valid_from} at={moment}",
        )
    if resolved.valid_to is not None and ensure_utc(resolved.valid_to) == moment:
        return resolved  # already closed at this instant - no-op
    closed = store.close_fact(resolved.id, moment, by_episode)
    if graph is not None:
        graph.invalidate_relationship(resolved.id, moment)  # SET r.valid_to; never DELETE
    logger.info(
        "temporal.fact_closed",
        fact_id=str(resolved.id),
        predicate=str(resolved.predicate),
        at=moment.isoformat(),
        reason=reason,
    )
    return closed


# --------------------------------------------------------------------------------------------------
# apply_fact() - temporal.md §4
# --------------------------------------------------------------------------------------------------


def apply_fact(
    new_fact: Fact,
    *,
    store: FactStore,
    episode_id: UUID | None = None,
    ontology: Ontology | None = None,
    graph: GraphSink | None = None,
    explicit_supersedes: UUID | None = None,
) -> TemporalOutcome:
    """Apply one fact under ADR-0005. Runs *after* entity resolution, *inside* the insert's
    transaction.

    ``explicit_supersedes`` carries rule 2 (an explicit supersession stated in the text, or
    ``record_decision(supersedes=...)``): it is authoritative and wins over every inference below.
    """
    onto = ontology or load_ontology()
    warnings: list[str] = []
    new_fact = _normalized(new_fact)

    # ---- 0. explicit supersession wins over everything (ADR-0005 rule 2) --------------------
    if explicit_supersedes is not None:
        target = store.get(explicit_supersedes)
        if target is None:
            warnings.append(
                f"stated supersession target {explicit_supersedes} not found; "
                "falling through to the inferred rules"
            )
        else:
            try:
                close(
                    target,
                    at=new_fact.valid_from,
                    by_episode=episode_id,
                    store=store,
                    reason="explicit",
                    graph=graph,
                )
            except TemporalRuleError as exc:
                warnings.append(f"explicit supersession refused: {exc.detail or exc}")
            else:
                stored = store.insert(
                    new_fact.model_copy(
                        update={
                            "supersedes_fact_id": target.id,
                            "status": FactStatus.CURRENT,
                            "valid_to": None,
                        }
                    )
                )
                return TemporalOutcome(
                    TemporalAction.EXPLICIT, stored, closed_fact_id=target.id, warnings=warnings
                )

    # ---- 1. functional predicates: one current object (ADR-0005 rule 1) --------------------
    if onto.is_functional(new_fact.predicate):
        open_fact = store.find_open_fact(new_fact.subject_entity_id, str(new_fact.predicate))

        if open_fact is None:
            stored = store.insert(new_fact.model_copy(update={"status": FactStatus.CURRENT}))
            return TemporalOutcome(TemporalAction.FIRST_VALUE, stored, warnings=warnings)

        if open_fact.object_key == new_fact.object_key:
            # Same value re-observed: refresh the row, do not create a second one. This also clears
            # an 'unconfirmed' flag set by rule 3, and keeps the unique index satisfiable.
            refreshed = store.touch(
                open_fact.id,
                observed_at=max(
                    ensure_utc(open_fact.observed_at), ensure_utc(new_fact.observed_at)
                ),
                status=str(FactStatus.CURRENT),
                confidence=max(open_fact.confidence, new_fact.confidence),
            )
            return TemporalOutcome(TemporalAction.RECONFIRMED, refreshed, warnings=warnings)

        if ensure_utc(new_fact.valid_from) < ensure_utc(open_fact.valid_from):
            # A late-discovered older statement must not close a newer one (temporal.md deviation 1).
            stored = store.insert(
                new_fact.model_copy(
                    update={
                        "status": FactStatus.HISTORICAL,
                        "valid_to": ensure_utc(open_fact.valid_from),
                    }
                )
            )
            return TemporalOutcome(TemporalAction.BACKDATED, stored, warnings=warnings)

        # close-then-insert, in this order, in one transaction: uq_facts_functional_current.
        close(
            open_fact,
            at=new_fact.valid_from,
            by_episode=episode_id,
            store=store,
            reason="functional",
            graph=graph,
        )
        stored = store.insert(
            new_fact.model_copy(
                update={
                    "supersedes_fact_id": open_fact.id,
                    "status": FactStatus.CURRENT,
                    "valid_to": None,
                }
            )
        )
        return TemporalOutcome(
            TemporalAction.SUPERSEDED, stored, closed_fact_id=open_fact.id, warnings=warnings
        )

    # ---- 2. non-functional predicates accumulate -------------------------------------------
    existing = store.find_open_fact_with_object(
        new_fact.subject_entity_id, str(new_fact.predicate), new_fact.object_key
    )
    if existing is not None:
        refreshed = store.touch(
            existing.id,
            observed_at=max(ensure_utc(existing.observed_at), ensure_utc(new_fact.observed_at)),
            status=str(FactStatus.CURRENT),
            confidence=max(existing.confidence, new_fact.confidence),
        )
        return TemporalOutcome(TemporalAction.RECONFIRMED, refreshed, warnings=warnings)

    stored = store.insert(new_fact.model_copy(update={"status": FactStatus.CURRENT}))
    return TemporalOutcome(TemporalAction.ADDED, stored, warnings=warnings)


def apply_facts(
    facts: Iterable[Fact],
    *,
    store: FactStore,
    episode_id: UUID | None = None,
    ontology: Ontology | None = None,
    graph: GraphSink | None = None,
    explicit: dict[UUID, UUID] | None = None,
) -> list[TemporalOutcome]:
    """:func:`apply_fact` over a sequence, keeping the caller's order.

    ``explicit`` maps a new fact's id to the id of the fact it explicitly supersedes.
    """
    onto = ontology or load_ontology()
    explicit = explicit or {}
    return [
        apply_fact(
            fact,
            store=store,
            episode_id=episode_id,
            ontology=onto,
            graph=graph,
            explicit_supersedes=explicit.get(fact.id),
        )
        for fact in facts
    ]


def _normalized(fact: Fact) -> Fact:
    """UTC-normalize the two time axes so comparisons never mix naive and aware values."""
    updates: dict[str, object] = {
        "valid_from": ensure_utc(fact.valid_from),
        "observed_at": ensure_utc(fact.observed_at),
    }
    if fact.valid_to is not None:
        updates["valid_to"] = ensure_utc(fact.valid_to)
    return fact.model_copy(update=updates)


# --------------------------------------------------------------------------------------------------
# Rule 3 - source edits never delete knowledge (temporal.md §5)
# --------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class ReconcileReport:
    """What rule 3 did between two versions of the same source."""

    unconfirmed: list[UUID] = field(default_factory=list)
    reconfirmed: list[UUID] = field(default_factory=list)
    contradicted: list[UUID] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "unconfirmed": len(self.unconfirmed),
            "reconfirmed": len(self.reconfirmed),
            "contradicted": len(self.contradicted),
        }


def reconcile_version(
    *,
    old_version_id: UUID,
    reextracted: Sequence[Fact] | Iterable[tuple[str, str, str]],
    store: FactStore,
    episode_id: UUID | None = None,
    ontology: Ontology | None = None,
    event_sink: Callable[[UUID, str, dict[str, str]], None] | None = None,
) -> ReconcileReport:
    """ADR-0005 rule 3, exactly as ``temporal.md`` §5 writes it.

    A fact from the previous version that the new version neither re-stated nor contradicted becomes
    ``unconfirmed``: ``valid_to`` stays ``NULL`` (it is still current), it is ranked down by
    ``boosts.unconfirmed_penalty`` and flagged in the context block. Only a contradiction or a user
    action ever closes it, and re-observing it later returns it to ``current`` through the
    ``reconfirmed`` branch of :func:`apply_fact`.

    ``reextracted`` accepts either the new :class:`Fact` objects or their fingerprints directly, so
    the caller may pass whichever it has.
    """
    onto = ontology or load_ontology()
    report = ReconcileReport()

    fingerprints: set[tuple[str, str, str]] = set()
    functional_subjects: set[tuple[str, str]] = set()
    for item in reextracted:
        if isinstance(item, Fact):
            fingerprints.add(fact_fingerprint(item))
            if onto.is_functional(item.predicate):
                functional_subjects.add((str(item.subject_entity_id), str(item.predicate)))
        else:
            subject, predicate, object_key = item
            fingerprints.add((subject, predicate, object_key))
            if onto.is_functional(predicate):
                functional_subjects.add((subject, predicate))

    for previous in store.facts_from_version(old_version_id):
        key = fact_fingerprint(previous)
        if key in fingerprints:
            report.reconfirmed.append(previous.id)
            continue  # apply_fact() already reconfirmed it
        if (key[0], key[1]) in functional_subjects:
            report.contradicted.append(previous.id)
            continue  # apply_fact() already closed it (functional, different object)
        store.set_status(previous.id, FactStatus.UNCONFIRMED)  # valid_to stays NULL
        report.unconfirmed.append(previous.id)
        if event_sink is not None and previous.provenance.source_id is not None:
            event_sink(
                previous.provenance.source_id,
                "modified",
                {"fact_id": str(previous.id), "to": "unconfirmed"},
            )

    logger.info(
        "temporal.version_reconciled",
        old_version=str(old_version_id),
        episode_id=str(episode_id) if episode_id else None,
        **report.counts,
    )
    return report


# --------------------------------------------------------------------------------------------------
# Rule 4 - source deletion flags, never erases (temporal.md §6)
# --------------------------------------------------------------------------------------------------


def mark_source_deleted(
    source_id: UUID,
    *,
    store: FactStore,
    event_sink: Callable[[UUID, str, dict[str, str]], None] | None = None,
    at: datetime | None = None,
) -> int:
    """Flag every derived row of a deleted source. ``valid_from``/``valid_to``/``status`` untouched.

    Returns the number of rows flagged. The ``sources.status='deleted'`` update itself belongs to
    A07a's change detection (``SourceRepo.mark_deleted``); this is the knowledge-side half, kept
    separate so a targeted re-flag does not require a scan.
    """
    flagged = store.mark_source_status(source_id, SourceStatus.DELETED)
    if event_sink is not None:
        event_sink(source_id, "deleted", {"derived_rows_flagged": str(flagged)})
    logger.info(
        "temporal.source_deleted_flagged",
        source_id=str(source_id),
        rows=flagged,
        at=(ensure_utc(at) if at else utc_now()).isoformat(),
    )
    return flagged


def restore_source(source_id: UUID, *, store: FactStore) -> int:
    """A deleted file that comes back: clear the flag. The knowledge never went anywhere."""
    return store.mark_source_status(source_id, SourceStatus.ACTIVE)
