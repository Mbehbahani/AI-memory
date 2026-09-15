"""P9-T01 (A09): ``SearchQuery`` -> SQL scoping predicates (``retrieval.md`` §1).

The scoping rules are security-relevant (plan section T: a ``secret_suspected`` source is never
returned, a ``CATALOG_ONLY`` source has no text to cite) and correctness-relevant (filtering after
``LIMIT 40`` silently empties a project-scoped query). This module pins the translation; the
``tests/integration/test_retrieval_candidates.py`` module pins that the SQL actually enforces it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aimemory.domain.enums import ArtifactType, StoragePolicy
from aimemory.domain.retrieval import SearchQuery
from aimemory.retrieval.filters import (
    ARTIFACT_PREDICATES,
    CHUNK_PREDICATES,
    READABLE_POLICIES,
    ScopeFilters,
)

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)


def test_default_scope_is_active_readable_unflagged_sources() -> None:
    scope = ScopeFilters.from_query(SearchQuery(query="pgvector"))

    assert scope.project_ids is None
    assert scope.allowed_source_status == ("active",)
    assert scope.allowed_policies == READABLE_POLICIES
    assert StoragePolicy.CATALOG_ONLY.value not in scope.allowed_policies
    assert StoragePolicy.IGNORE.value not in scope.allowed_policies


def test_include_deleted_sources_widens_the_status_set_rather_than_removing_the_filter() -> None:
    scope = ScopeFilters.from_query(SearchQuery(query="q", include_deleted_sources=True))

    assert set(scope.allowed_source_status) == {"active", "deleted", "moved"}


def test_project_and_artifact_type_scoping_is_passed_through() -> None:
    query = SearchQuery(
        query="q", project_ids=["joblab-de"], artifact_types=[ArtifactType.DECISION]
    )

    scope = ScopeFilters.from_query(query)

    assert scope.project_ids == ["joblab-de"]
    assert scope.artifact_types == ["decision"]


def test_as_of_defaults_to_now_and_since_stays_optional() -> None:
    scope = ScopeFilters.from_query(SearchQuery(query="q"))

    assert scope.as_of is None
    assert scope.since is None
    assert scope.effective_as_of() is not None  # now(), per retrieval.md §5


def test_naive_timestamps_are_normalised_to_utc() -> None:
    naive = datetime(2026, 1, 1, 0, 0, 0)  # noqa: DTZ001 - a naive value is the point here

    scope = ScopeFilters.from_query(SearchQuery(query="q", as_of=naive, since=naive))

    assert scope.as_of is not None and scope.as_of.tzinfo is not None
    assert scope.since is not None and scope.since.tzinfo is not None


def test_bind_params_cover_every_named_parameter_used_by_the_predicates() -> None:
    scope = ScopeFilters.from_query(SearchQuery(query="q", as_of=NOW))
    params = scope.bind_params()

    for fragment in (CHUNK_PREDICATES, ARTIFACT_PREDICATES):
        for name in ("project_ids", "allowed_source_status", "allowed_policies", "since"):
            if f":{name}" in fragment:
                assert name in params, f"{name} is used in SQL but not bound"
    assert {"as_of", "include_unconfirmed", "artifact_types"} <= set(params)


def test_secret_suspected_and_policy_are_filtered_in_sql_not_afterwards() -> None:
    # Plan section T is only verifiable at the query level if these live in the WHERE clause.
    assert "secret_suspected = false" in CHUNK_PREDICATES
    assert "secret_suspected = false" in ARTIFACT_PREDICATES
    assert "s.policy = ANY" in CHUNK_PREDICATES


def test_the_artifact_predicate_carries_the_adr_0005_point_in_time_clause() -> None:
    assert "a.valid_from <= CAST(:as_of AS timestamptz)" in ARTIFACT_PREDICATES
    assert "a.valid_to IS NULL OR a.valid_to > CAST(:as_of AS timestamptz)" in ARTIFACT_PREDICATES


def test_since_is_evaluated_on_the_observation_axis_not_on_ingestion_time() -> None:
    # temporal.md §8 / deviation 5: "since" answers "what changed lately", which is observed_at.
    assert "sv.observed_at >= CAST(:since AS timestamptz)" in CHUNK_PREDICATES
    assert "c.created_at" not in CHUNK_PREDICATES
    assert "a.observed_at >= CAST(:since AS timestamptz)" in ARTIFACT_PREDICATES
