"""P10-T01 (A09): stage 5, the temporal filter over ranked hits (ADR-0005 §5, ``retrieval.md`` §5).

The fact/artifact SQL is exercised against a real database in
``tests/integration/test_gateway_search.py``; what is pinned here is the in-memory predicate that
runs *after* ranking, because it is the one that decides whether a superseded decision can still be
returned to a caller.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from aimemory.domain.enums import ObjectType
from aimemory.retrieval.temporal import filter_hits
from aimemory.retrieval.types import HitMetadata, RankedCandidate

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:p10-temporal:{label}")


def _hit(
    label: str,
    *,
    object_type: ObjectType = ObjectType.ARTIFACT,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
    status: str | None = "current",
) -> RankedCandidate:
    return RankedCandidate(
        object_type=object_type,
        object_id=_id(label),
        rrf_score=0.5,
        score=0.5,
        rank=1,
        metadata=HitMetadata(
            object_type=object_type,
            object_id=_id(label),
            text=f"text {label}",
            observed_at=NOW,
            valid_from=valid_from,
            valid_to=valid_to,
            status=status,
        ),
    )


def test_an_artifact_closed_before_as_of_is_dropped() -> None:
    closed = _hit("closed", valid_from=NOW - timedelta(days=100), valid_to=NOW - timedelta(days=10))
    open_now = _hit("open", valid_from=NOW - timedelta(days=100))

    kept, dropped = filter_hits([closed, open_now], as_of=NOW)

    assert [hit.object_id for hit in kept] == [open_now.object_id]
    assert [hit.object_id for hit in dropped] == [closed.object_id]


def test_the_same_artifact_is_visible_at_an_as_of_inside_its_window() -> None:
    closed = _hit("closed", valid_from=NOW - timedelta(days=100), valid_to=NOW - timedelta(days=10))

    kept, dropped = filter_hits([closed], as_of=NOW - timedelta(days=50))

    assert [hit.object_id for hit in kept] == [closed.object_id]
    assert dropped == []


def test_an_artifact_not_yet_valid_is_dropped() -> None:
    future = _hit("future", valid_from=NOW + timedelta(days=5))

    kept, dropped = filter_hits([future], as_of=NOW)

    assert kept == []
    assert len(dropped) == 1


def test_a_null_valid_from_counts_as_always_started() -> None:
    """Rows written before a temporal engine ran must not silently vanish (ADR-0005)."""
    legacy = _hit("legacy", valid_from=None, valid_to=None)

    kept, _ = filter_hits([legacy], as_of=NOW)

    assert [hit.object_id for hit in kept] == [legacy.object_id]


def test_chunks_are_never_temporally_filtered() -> None:
    """Chunks are immutable text; their source's status is what scopes them (``retrieval.md`` §5)."""
    chunk = _hit("chunk", object_type=ObjectType.CHUNK, valid_to=NOW - timedelta(days=1))

    kept, dropped = filter_hits([chunk], as_of=NOW)

    assert [hit.object_id for hit in kept] == [chunk.object_id]
    assert dropped == []


@pytest.mark.parametrize("include", [True, False])
def test_unconfirmed_artifacts_are_kept_by_default_and_droppable_on_request(include: bool) -> None:
    unconfirmed = _hit("unconfirmed", valid_from=NOW - timedelta(days=1), status="unconfirmed")

    kept, dropped = filter_hits([unconfirmed], as_of=NOW, include_unconfirmed=include)

    assert bool(kept) is include
    assert bool(dropped) is (not include)


def test_ranks_are_renumbered_so_there_is_never_a_hole_in_the_list() -> None:
    hits = [
        _hit("a", valid_from=NOW - timedelta(days=1)),
        _hit("b", valid_from=NOW - timedelta(days=100), valid_to=NOW - timedelta(days=2)),
        _hit("c", valid_from=NOW - timedelta(days=1)),
    ]

    kept, _ = filter_hits(hits, as_of=NOW)

    assert [hit.rank for hit in kept] == [1, 2]
