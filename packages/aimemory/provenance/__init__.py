"""``aimemory.provenance`` - plan section J: the stamp and the chain (owner A08).

Two halves:

* :mod:`~aimemory.provenance.builders` mints the fourteen ``[PROV]`` columns for every derived row,
  once per episode, so no call site can forget one;
* :mod:`~aimemory.provenance.explain` walks the chain back
  ``fact|artifact|mention -> episode -> version -> source -> root -> device + models + run`` out of
  PostgreSQL alone (ADR-0001), which is what ``Gateway.explain(id)`` and ``memory.explain`` serve.

``PROVENANCE_COLUMNS`` in :mod:`aimemory.domain.provenance` is the authoritative order; everything
here renders in it.
"""

from ..domain.provenance import (
    DETERMINISTIC_MODEL_ID,
    PROVENANCE_COLUMNS,
    Provenance,
    ProvenanceChainStep,
)
from .builders import (
    SOURCE_DERIVED_REQUIRED,
    EpisodeProvenance,
    build_provenance,
    deterministic_provenance,
    missing_provenance_columns,
    provenance_payload,
)
from .explain import ExplainResult, explain, explain_many, provenance_completeness

__all__ = [
    "DETERMINISTIC_MODEL_ID",
    "PROVENANCE_COLUMNS",
    "SOURCE_DERIVED_REQUIRED",
    "EpisodeProvenance",
    "ExplainResult",
    "Provenance",
    "ProvenanceChainStep",
    "build_provenance",
    "deterministic_provenance",
    "explain",
    "explain_many",
    "missing_provenance_columns",
    "provenance_completeness",
    "provenance_payload",
]
