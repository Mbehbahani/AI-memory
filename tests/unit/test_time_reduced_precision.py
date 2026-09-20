"""`parse_timestamp` must accept the reduced-precision ISO 8601 dates real documents contain.

MEASURED on the real vault: seven career documents failed Tier 2 extraction outright with
`ValueError: Invalid isoformat string: '2022-03'` (also '2018', '2026-07'). CVs and portfolios state
dates to the year or the month, which ISO 8601 allows and `datetime.fromisoformat` rejects. The
exception escaped into the persistence step and took the whole episode with it - every other fact in
that document lost along with the one coarse date.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aimemory.common.time import parse_timestamp


@pytest.mark.parametrize(
    ("stated", "expected"),
    [
        ("2018", datetime(2018, 1, 1, tzinfo=UTC)),
        ("2022-03", datetime(2022, 3, 1, tzinfo=UTC)),
        ("2026-07", datetime(2026, 7, 1, tzinfo=UTC)),
        ("2022-12", datetime(2022, 12, 1, tzinfo=UTC)),
        ("2022-01", datetime(2022, 1, 1, tzinfo=UTC)),
    ],
)
def test_reduced_precision_resolves_to_the_earliest_instant_it_admits(stated, expected) -> None:
    """March 2022 did begin on 2022-03-01. Coercing to the earliest instant keeps the stated
    precision honest, where defaulting to `now()` would assert a decade-old CV line became true
    today, and dropping it would discard information the document really contains."""
    assert parse_timestamp(stated) == expected


@pytest.mark.parametrize(
    ("stated", "expected"),
    [
        ("2026-09-14", datetime(2026, 9, 14, tzinfo=UTC)),
        ("2026-09-14T10:00:00Z", datetime(2026, 9, 14, 10, 0, tzinfo=UTC)),
        ("2026-09-14T10:00:00+00:00", datetime(2026, 9, 14, 10, 0, tzinfo=UTC)),
    ],
)
def test_full_precision_is_unchanged(stated, expected) -> None:
    assert parse_timestamp(stated) == expected


@pytest.mark.parametrize("stated", ["2022-13", "2022-00", "not-a-date", "20221", "22-03", "2022-3"])
def test_garbage_still_raises(stated) -> None:
    """The widening must not become a rubber stamp: an unparseable LLM date is still a validation
    failure, which is the whole reason this function raises rather than guessing."""
    with pytest.raises(ValueError):
        parse_timestamp(stated)


@pytest.mark.parametrize("stated", [None, "", "   "])
def test_absent_values_are_none_not_errors(stated) -> None:
    assert parse_timestamp(stated) is None
