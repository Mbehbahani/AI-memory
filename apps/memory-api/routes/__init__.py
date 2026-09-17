"""memory-api route modules (plan section Q). Owner: A09.

``search`` is the retrieval pipeline; ``read`` is every registry-shaped GET; ``write`` holds the two
ADR-0008-gated POSTs; ``system`` is ``/health`` and ``/metrics``. P12-T02 adds ``ops`` (A16) next to
these and mounts it the same way.
"""

from . import read, search, system, write

__all__ = ["read", "search", "system", "write"]
