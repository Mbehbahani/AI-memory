"""Inline SVG trend charts for the Quality section (ADR-0010 metrics). No JS, no canvas, no CDN -
just an ``<svg>`` string the template drops straight into the page, which is what "offline" and "no
build step" (ADR-0011) mean in practice for a chart.

Dataviz choices made deliberately, not by default:

* **Rate/share metrics** (validity, failed-episode share, duplicate-entity rate, unconfirmed-fact
  share) are plotted on a fixed 0-100% axis, never an auto-scaled one - a metric that moved from 96%
  to 94% must not fill the whole chart height and look like a collapse.
* **Unbounded metrics** (seconds/episode) get a data-driven axis, starting at zero, because there is
  no natural ceiling; the axis maximum and both end labels are drawn on the chart itself so the shape
  is never read without its scale.
* Fewer than two points is not a chart - :func:`sparkline` returns an honest "not enough data yet"
  placeholder instead of a single dot pretending to be a trend.
* Every chart states n (point count) and the first/last timestamp in a caption under the ``<svg>``,
  so a screen reader or a text-only view still gets the numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from .viewmodels import SnapshotPoint

__all__ = ["EMPTY_CHART_HTML", "sparkline"]

EMPTY_CHART_HTML = '<p class="chart-empty">not enough data yet - run a scan to populate this metric</p>'

_WIDTH = 320
_HEIGHT = 72
_PAD_X = 8
_PAD_Y = 10


def _scale(value: float, lo: float, hi: float, out_lo: float, out_hi: float) -> float:
    if hi <= lo:
        return (out_lo + out_hi) / 2
    return out_lo + (value - lo) / (hi - lo) * (out_hi - out_lo)


def sparkline(
    points: Sequence[SnapshotPoint],
    *,
    title: str,
    unit: str = "",
    as_percent: bool = False,
    fixed_domain: tuple[float, float] | None = None,
) -> str:
    """One inline ``<svg>`` line chart. ``fixed_domain`` overrides auto-scaling (used for 0-100% rate
    metrics per the module docstring); leave unset for a data-driven, zero-based axis."""
    usable = [p for p in points if p.value is not None]
    if len(usable) < 2:
        return EMPTY_CHART_HTML

    values: list[float] = [
        (p.value * 100 if as_percent else p.value) for p in usable if p.value is not None
    ]
    lo, hi = (fixed_domain if fixed_domain else (0.0, max(values)))
    if hi <= lo:
        hi = lo + 1.0

    n = len(values)
    xs = [_scale(i, 0, n - 1, _PAD_X, _WIDTH - _PAD_X) for i in range(n)]
    ys = [_scale(v, lo, hi, _HEIGHT - _PAD_Y, _PAD_Y) for v in values]  # inverted: higher value = up
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))
    last_x, last_y = xs[-1], ys[-1]

    fmt = (lambda v: f"{v:.1f}%") if as_percent else (lambda v: f"{v:.2f}{unit}")
    first_at = usable[0].at.strftime("%Y-%m-%d %H:%M")
    last_at = usable[-1].at.strftime("%Y-%m-%d %H:%M")

    svg = (
        f'<svg class="sparkline" viewBox="0 0 {_WIDTH} {_HEIGHT}" width="{_WIDTH}" height="{_HEIGHT}" '
        f'role="img" aria-label="{escape(title)} trend, {n} points, {fmt(values[0])} to {fmt(values[-1])}">'
        f'<polyline points="{poly}" fill="none" stroke="currentColor" stroke-width="1.5" />'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.5" fill="currentColor" />'
        f"</svg>"
    )
    caption = (
        f'<p class="chart-caption">{escape(title)}: {fmt(values[0])} &rarr; {fmt(values[-1])} '
        f"(n={n}, {escape(first_at)} to {escape(last_at)} UTC)</p>"
    )
    return svg + caption
