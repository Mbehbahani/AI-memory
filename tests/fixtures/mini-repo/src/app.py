"""Entry point for the mini-repo fixture. Deliberately longer than 60 lines so the code chunker
(target ~60 lines per chunk) actually has to produce more than one window and snap to a boundary.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .models import EventBatch
from .utils import clamp, load_events


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mini-repo")
    parser.add_argument("events_csv", type=Path)
    parser.add_argument("--min-value", type=float, default=0.0)
    parser.add_argument("--max-value", type=float, default=1_000_000.0)
    return parser


def summarize(batch: EventBatch, min_value: float, max_value: float) -> dict[str, float]:
    total = 0.0
    count = 0
    for event in batch.events:
        if not event.is_valid():
            continue
        clamped = clamp(event.value, min_value, max_value)
        total += clamped
        count += 1
    average = total / count if count else 0.0
    return {"total": total, "count": count, "average": average}


def print_summary(summary: dict[str, float]) -> None:
    print(f"total={summary['total']:.2f}")
    print(f"count={int(summary['count'])}")
    print(f"average={summary['average']:.2f}")


class SummaryReport:
    """Wraps :func:`summarize` output with a couple of derived fields, for the OO-vs-function mix
    this fixture wants to exercise (both top-level ``def`` and top-level ``class`` boundaries)."""

    def __init__(self, summary: dict[str, float]) -> None:
        self.summary = summary

    @property
    def is_empty(self) -> bool:
        return self.summary["count"] == 0

    def as_line(self) -> str:
        if self.is_empty:
            return "no valid events"
        return (
            f"{int(self.summary['count'])} events, "
            f"total={self.summary['total']:.2f}, "
            f"average={self.summary['average']:.2f}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.events_csv.is_file():
        print(f"not found: {args.events_csv}", file=sys.stderr)
        return 2
    batch = load_events(args.events_csv)
    summary = summarize(batch, args.min_value, args.max_value)
    print_summary(summary)
    report = SummaryReport(summary)
    print(report.as_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
