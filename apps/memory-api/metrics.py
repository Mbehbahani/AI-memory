"""``/metrics``: simple in-process counters (plan section Q). Owner: A09.

Deliberately *simple*: a dict of counters updated by one middleware, with no Prometheus client, no
histogram buckets and no scrape protocol. V0.1 is a single local process; the Ops page (ADR-0011)
and the evaluation report are the consumers, and both read JSON.

Everything reported here is **MEASURED since process start**. There are no targets, no estimates and
no derived rates - :attr:`Counters.latency_ms` is a mean over the requests this process has served,
labelled as such, and callers that need percentiles read ``retrieval_logs`` instead, where every
query's latency is stored individually.
"""

from __future__ import annotations

import threading
import time
from collections import Counter as _Counter
from dataclasses import dataclass, field

__all__ = ["COUNTERS", "Counters"]


@dataclass
class Counters:
    """Thread-safe request/response/error counters for one process."""

    started_at: float = field(default_factory=time.monotonic)
    requests: _Counter[str] = field(default_factory=_Counter)
    responses: _Counter[str] = field(default_factory=_Counter)
    errors: _Counter[str] = field(default_factory=_Counter)
    warnings: _Counter[str] = field(default_factory=_Counter)
    latency_sum_ms: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record_request(self, route: str) -> None:
        with self._lock:
            self.requests[route] += 1

    def record_response(self, route: str, status: int, latency_ms: float) -> None:
        with self._lock:
            self.responses[f"{route} {status}"] += 1
            self.latency_sum_ms[route] = self.latency_sum_ms.get(route, 0.0) + latency_ms

    def record_error(self, code: str) -> None:
        with self._lock:
            self.errors[code] += 1

    def record_warnings(self, warnings: list[str]) -> None:
        """Degraded-mode notices are counted, so "how often did the graph go away?" is answerable."""
        with self._lock:
            for warning in warnings:
                self.warnings[warning] += 1

    @property
    def uptime_seconds(self) -> float:
        return max(time.monotonic() - self.started_at, 0.0)

    def mean_latency_ms(self) -> dict[str, float]:
        """Mean latency per route - MEASURED, and only meaningful next to ``requests_total``."""
        with self._lock:
            return {
                route: round(total / max(self.requests[route], 1), 2)
                for route, total in self.latency_sum_ms.items()
            }

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                "requests_total": dict(self.requests),
                "responses_total": dict(self.responses),
                "errors_total": dict(self.errors),
                "warnings_total": dict(self.warnings),
            }


#: The process-wide counter set. One per process; tests build their own.
COUNTERS = Counters()
