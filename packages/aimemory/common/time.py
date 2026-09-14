"""UTC time helpers for the AI Memory temporal model (ADR-0005).

Consumers: A04 (migrations/repositories), A07a/A07b (ingestion), A08 (temporal engine),
A09 (retrieval `as_of`/`since` filters), A10 (MCP argument coercion), A12 (tests).

Rules encoded here (ADR-0005 §6 and plan §I):

* Every timestamp stored or compared in the system is timezone-aware UTC.
* ``observed_at`` is the *source* time — file mtime or git commit time when available, otherwise the
  ingestion-run start time.
* ``valid_from`` defaults to ``observed_at`` unless the text states an explicit date.
* Point-in-time predicate: ``valid_from <= as_of AND (valid_to IS NULL OR valid_to > as_of)``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

__all__ = [
    "EPOCH",
    "UTC",
    "ensure_utc",
    "is_valid_at",
    "isoformat_utc",
    "observed_at_for",
    "parse_timestamp",
    "utc_now",
    "valid_from_for",
]

#: Re-exported so callers write ``from aimemory.common.time import UTC`` and never build a
#: naive datetime by accident.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utc_now() -> datetime:
    """Current time as an aware UTC datetime. The single clock used by the whole system."""
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as an aware UTC datetime.

    A naive datetime is *assumed* to be UTC (databases and file systems hand us naive values);
    an aware datetime in another zone is converted.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_timestamp(value: str | datetime | date | None) -> datetime | None:
    """Parse an ISO-8601 string / date / datetime into an aware UTC datetime.

    Accepts the ``Z`` suffix and bare dates (``2026-09-14`` → midnight UTC). Returns ``None`` for
    ``None`` or an empty string. Raises :class:`ValueError` on anything else, so callers can turn an
    LLM-stated date into a validation failure rather than a silent wrong answer.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return ensure_utc(datetime.fromisoformat(text))


def isoformat_utc(value: datetime) -> str:
    """Canonical serialization: ``2026-09-14T10:32:00+00:00`` (aware, UTC, microseconds kept)."""
    return ensure_utc(value).isoformat()


def observed_at_for(
    source_time: datetime | None,
    run_started_at: datetime | None = None,
) -> datetime:
    """ADR-0005 §6: source time (mtime / commit time) if known, else run time, else now."""
    if source_time is not None:
        return ensure_utc(source_time)
    if run_started_at is not None:
        return ensure_utc(run_started_at)
    return utc_now()


def valid_from_for(stated: datetime | str | None, observed_at: datetime) -> datetime:
    """ADR-0005 §6: an explicitly stated date wins; otherwise ``valid_from = observed_at``."""
    parsed = parse_timestamp(stated)
    return parsed if parsed is not None else ensure_utc(observed_at)


def is_valid_at(
    valid_from: datetime | None,
    valid_to: datetime | None,
    as_of: datetime | None = None,
) -> bool:
    """ADR-0005 §5 point-in-time predicate, in Python, for tests and in-memory filtering.

    ``valid_from IS NULL`` is treated as "always started" so that rows written before a temporal
    engine ran are never silently dropped from an ``as_of`` query.
    """
    moment = ensure_utc(as_of) if as_of is not None else utc_now()
    started = valid_from is None or ensure_utc(valid_from) <= moment
    still_open = valid_to is None or ensure_utc(valid_to) > moment
    return started and still_open
