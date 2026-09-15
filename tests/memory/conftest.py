"""Fixtures for the memory-scenario suite (P6-T04, owner A07a with A12).

Only fixtures live here; the deterministic knowledge engine and the ADR-0005 writer they hand out are
in :mod:`scenario_support`, which the test module imports directly as well. See that module's
docstring for what the stub does and does not prove.
"""

from __future__ import annotations

import pytest
from scenario_support import StubKnowledgeEngine


@pytest.fixture()
def stub_engine() -> StubKnowledgeEngine:
    """A fresh :class:`StubKnowledgeEngine` per test (it records the episodes it saw)."""
    return StubKnowledgeEngine()
