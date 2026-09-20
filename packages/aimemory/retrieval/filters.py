"""The SQL scoping filters shared by both candidate retrievers (``retrieval.md`` §1/§2).

Two rules drive this module.

**The filters live inside the SQL, not after it.** Filtering a ``LIMIT 40`` candidate list in Python
silently empties a project-scoped query, and it would make plan section T's "flagged files are never
returned" unverifiable at the query level.

**A search sees exactly one version of any file** - the one in force at ``as_of``. See
:data:`CURRENT_VERSION_PREDICATE`; this was missing, and every version of an edited file stayed
retrievable forever.

:class:`ScopeFilters` is the single place the ``SearchQuery`` knobs become SQL predicates, so the
semantic and the keyword retriever can never drift apart on what they consider visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..common.time import ensure_utc, utc_now
from ..domain.enums import ArtifactType, SourceStatus, StoragePolicy
from ..domain.retrieval import SearchQuery

__all__ = [
    "ARTIFACT_PREDICATES",
    "CHUNK_PREDICATES",
    "CURRENT_VERSION_PREDICATE",
    "READABLE_POLICIES",
    "ScopeFilters",
    "artifact_type_values",
]

#: Only these two policies have stored text to retrieve and cite; ``CATALOG_ONLY`` sources are
#: registered but have no ``source_text`` row, and ``IGNORE`` sources are never registered at all.
READABLE_POLICIES: tuple[str, ...] = (StoragePolicy.INDEX_CONTENT.value, StoragePolicy.MIRROR.value)


@dataclass(frozen=True, slots=True)
class ScopeFilters:
    """Resolved scoping for one query. ``None`` for ``project_ids``/``since`` means "no filter"."""

    project_ids: list[str] | None = None
    allowed_source_status: tuple[str, ...] = (SourceStatus.ACTIVE.value,)
    allowed_policies: tuple[str, ...] = READABLE_POLICIES
    since: datetime | None = None
    as_of: datetime | None = None
    include_unconfirmed: bool = True
    artifact_types: list[str] | None = None

    @classmethod
    def from_query(cls, query: SearchQuery) -> ScopeFilters:
        """Translate a :class:`~aimemory.domain.retrieval.SearchQuery` into scoping predicates.

        ``include_deleted_sources`` widens the status set rather than removing the filter, so a
        ``moved`` source (which keeps ``status='active'``, see ``SourceRepo.mark_moved``) is always
        visible and a ``deleted`` one only on request - ADR-0005 rule 4 flags, never erases.
        """
        statuses: tuple[str, ...] = (SourceStatus.ACTIVE.value,)
        if query.include_deleted_sources:
            statuses = tuple(s.value for s in SourceStatus)
        return cls(
            project_ids=list(query.project_ids) or None,
            allowed_source_status=statuses,
            allowed_policies=READABLE_POLICIES,
            since=ensure_utc(query.since) if query.since else None,
            as_of=ensure_utc(query.as_of) if query.as_of else None,
            include_unconfirmed=query.include_unconfirmed,
            artifact_types=[t.value for t in query.artifact_types] or None,
        )

    # ------------------------------------------------------------------------------------- SQL

    def effective_as_of(self) -> datetime:
        """``as_of`` defaults to now() (``retrieval.md`` §5)."""
        return self.as_of or utc_now()

    def bind_params(self) -> dict[str, Any]:
        """Bind values shared by every candidate query in this module's SQL fragments."""
        return {
            "project_ids": self.project_ids,
            "allowed_source_status": list(self.allowed_source_status),
            "allowed_policies": list(self.allowed_policies),
            "since": self.since,
            "as_of": self.effective_as_of(),
            "include_unconfirmed": self.include_unconfirmed,
            "artifact_types": self.artifact_types,
        }


def artifact_type_values() -> tuple[str, ...]:
    """Every legal ``knowledge_artifacts.type`` - used by tests to catch an enum drift early."""
    return tuple(t.value for t in ArtifactType)


#: One version per source, and only one: the newest observed at or before ``as_of``.
#:
#: Without this, every version of a file that had ever been indexed stayed retrievable forever, each
#: with its own embeddings. MEASURED on the live corpus before this predicate existed: an edited vault
#: document had 4 chunks from its 04:45 version and 4 from its 18:20 version, all 8 embedded and all 8
#: searchable, nothing marking either as superseded - so a search could quote text that no longer
#: existed on disk and cite it as current. Re-scanning did not repair that; re-scanning caused it,
#: because each edit added another retrievable copy.
#:
#: The data was never wrong - ``source_versions.is_current`` matched ``sources.current_version_id`` on
#: all 241 sources. Retrieval simply never asked.
#:
#: ``AND sv.is_current`` would fix the default case and break the other one: ``SearchQuery.as_of``
#: asks "what did I know at time T", and answering that with today's file contents is its own kind of
#: wrong. Selecting the newest version at or before ``as_of`` collapses to ``is_current`` when
#: ``as_of`` is now (the default, every ordinary search) and returns the text that was really in the
#: file otherwise - matching how facts and artifacts already honour ``valid_from``/``valid_to``.
#:
#: The tie-break on ``(observed_at, id)`` rather than ``observed_at`` alone guarantees *exactly* one
#: winner even if two versions were ever recorded with identical timestamps; without it a tie would
#: silently reintroduce the duplicate this predicate exists to remove.
CURRENT_VERSION_PREDICATE = """
          AND sv.observed_at <= CAST(:as_of AS timestamptz)
          AND NOT EXISTS (
              SELECT 1
                FROM source_versions sv2
               WHERE sv2.source_id = sv.source_id
                 AND sv2.observed_at <= CAST(:as_of AS timestamptz)
                 AND (sv2.observed_at, sv2.id) > (sv.observed_at, sv.id)
          )
"""

#: Predicates for the ``chunks c`` / ``sources s`` / ``source_versions sv`` join.
#:
#: ``since`` is evaluated against ``sv.observed_at`` and not ``c.created_at``: ``temporal.md`` §8
#: fixes ``since`` on the *observation* axis ("what changed lately"), while ``chunks.created_at`` is
#: ingestion time. ``retrieval.md`` §1 agreed with this code as of commit ff19689 (A02 corrected the
#: doc after P9-T01 reported the drift).
CHUNK_PREDICATES = (
    """
          AND (
              CAST(:project_ids AS text[]) IS NULL
              OR c.project_id = ANY(CAST(:project_ids AS text[]))
          )
          AND s.status = ANY(CAST(:allowed_source_status AS text[]))
          AND s.policy = ANY(CAST(:allowed_policies AS text[]))
          AND s.secret_suspected = false
          AND (
              CAST(:since AS timestamptz) IS NULL
              OR sv.observed_at >= CAST(:since AS timestamptz)
          )
"""
    + CURRENT_VERSION_PREDICATE
)

#: Predicates for ``knowledge_artifacts a`` LEFT JOINed to ``sources s``.
#:
#: The ADR-0005 §5 point-in-time predicate is applied *here* rather than after ``LIMIT`` for the
#: same reason as every other filter (``retrieval.md`` §1: "the filters are inside the SQL").
#: ``a.source_status`` is the artifact's own column (``temporal.md`` deviation 4), which is why a
#: missing ``sources`` row is not fatal.
ARTIFACT_PREDICATES = """
          AND (
              CAST(:project_ids AS text[]) IS NULL
              OR a.project_id = ANY(CAST(:project_ids AS text[]))
          )
          AND a.source_status = ANY(CAST(:allowed_source_status AS text[]))
          AND (s.id IS NULL OR s.policy = ANY(CAST(:allowed_policies AS text[])))
          AND (s.id IS NULL OR s.secret_suspected = false)
          AND (
              CAST(:since AS timestamptz) IS NULL
              OR a.observed_at >= CAST(:since AS timestamptz)
          )
          AND a.valid_from <= CAST(:as_of AS timestamptz)
          AND (a.valid_to IS NULL OR a.valid_to > CAST(:as_of AS timestamptz))
          AND (CAST(:include_unconfirmed AS boolean) OR a.current_status <> 'unconfirmed')
          AND (
              CAST(:artifact_types AS text[]) IS NULL
              OR a.type = ANY(CAST(:artifact_types AS text[]))
          )
"""
