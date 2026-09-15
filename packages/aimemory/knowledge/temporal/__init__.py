"""``aimemory.knowledge.temporal`` - ADR-0005 rules 1-6 (owner A08, spec ``docs/architecture/temporal.md``).

* :func:`apply_fact` - rules 1, 2 and the backdated / reconfirmed branches
* :func:`close` - the ``KnowledgeEngine.invalidate`` entry point; idempotent
* :func:`reconcile_version` - rule 3 (``unconfirmed``, never deleted)
* :func:`mark_source_deleted` - rule 4 (flag, never erase)
* :func:`apply_artifact` - rule 6 (supersession is a state)
* :func:`is_valid_at` - rule 5, re-exported from :mod:`aimemory.common.time` so the point-in-time
  predicate has exactly one definition in the codebase

Nothing here deletes a row, and nothing here commits: the caller owns the transaction, because the
functional close-then-insert pair must be atomic against ``uq_facts_functional_current``.
"""

from ...common.time import is_valid_at
from .artifacts import (
    ArtifactOutcome,
    ArtifactStore,
    SqlArtifactStore,
    apply_artifact,
    reconcile_artifacts,
)
from .rules import (
    ReconcileReport,
    TemporalAction,
    TemporalOutcome,
    TemporalRuleError,
    apply_fact,
    apply_facts,
    close,
    mark_source_deleted,
    reconcile_version,
    restore_source,
)
from .store import FactStore, GraphSink, SqlFactStore, fact_fingerprint, fact_from_row

__all__ = [
    "ArtifactOutcome",
    "ArtifactStore",
    "FactStore",
    "GraphSink",
    "ReconcileReport",
    "SqlArtifactStore",
    "SqlFactStore",
    "TemporalAction",
    "TemporalOutcome",
    "TemporalRuleError",
    "apply_artifact",
    "apply_fact",
    "apply_facts",
    "close",
    "fact_fingerprint",
    "fact_from_row",
    "is_valid_at",
    "mark_source_deleted",
    "reconcile_artifacts",
    "reconcile_version",
    "restore_source",
]
