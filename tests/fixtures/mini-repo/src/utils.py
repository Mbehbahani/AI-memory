"""Small utility functions for the mini-repo fixture."""

from __future__ import annotations

import csv
from pathlib import Path

from .models import Event, EventBatch


def load_events(path: Path) -> EventBatch:
    """Read ``events.csv`` into an :class:`~mini_repo.models.EventBatch`."""
    events: list[Event] = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            events.append(
                Event(id=int(row["id"]), name=row["name"], value=float(row["value"]))
            )
    return EventBatch(events=events)


def clamp(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def chunked(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]
