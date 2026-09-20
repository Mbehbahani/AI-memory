"""``aimemory.ops`` - read models and view models for the `/ops` dashboard (ADR-0011, P12-T02).

Owner: A16. This package holds no HTTP concerns (that is ``apps/memory-api/routes/ops.py``) and no
new writes to the domain tables A04 owns (``sources``, ``episodes``, ``facts``, ...): every function
here either reads, or appends exactly one row to one of the four ADR-0010/ADR-0011 tables
(``run_requests``, ``extraction_reviews``) through :mod:`aimemory.persistence.repositories.MetricsRepo`
- the same insert path the CLI and the worker use, so the Ops page can never write anything the
worker does not already understand.

Layout:

=====================================  ================================================
Module                                 Contents
=====================================  ================================================
:mod:`aimemory.ops.queries`            Read-only SQL behind every dashboard section
:mod:`aimemory.ops.viewmodels`         Plain dataclasses the templates render
:mod:`aimemory.ops.charts`             Inline SVG sparklines/trend lines (no JS, no CDN)
:mod:`aimemory.ops.actions`            The two writes: enqueue a run, record a review verdict
=====================================  ================================================
"""

from __future__ import annotations

__all__: list[str] = []
