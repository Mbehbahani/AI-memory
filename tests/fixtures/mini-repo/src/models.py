"""Tiny data models for the mini-repo fixture. No real logic - just enough shape for the code
chunker's boundary-snapping (blank lines + top-level ``class``/``def``) to have something to work
with.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Event:
    """One fixture event row."""

    id: int
    name: str
    value: float

    def is_valid(self) -> bool:
        return self.id >= 0 and bool(self.name)


@dataclass
class EventBatch:
    """A batch of :class:`Event` rows, as read from ``data/events.csv``."""

    events: list[Event]

    def total_value(self) -> float:
        return sum(e.value for e in self.events if e.is_valid())

    def filter_by_name(self, name: str) -> "EventBatch":
        return EventBatch(events=[e for e in self.events if e.name == name])


def empty_batch() -> EventBatch:
    return EventBatch(events=[])
