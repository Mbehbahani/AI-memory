"""The read routes of plan section Q: projects, entities, related, decisions, timeline, sources,
artifacts, state and explain. Owner: A09.

Every one of these is a one-line delegation to :class:`~aimemory.gateway.Gateway`. That is the point:
scoping, the ``as_of`` predicate, provenance and the "URIs, never bytes" rule are enforced in the
service, so a route cannot forget one of them, and MCP (which calls these same functions through
this same API) inherits them for free.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from aimemory.domain.retrieval import ExplainChain
from aimemory.gateway import (
    ArtifactView,
    CurrentState,
    EntityView,
    Gateway,
    ProjectView,
    RelatedResult,
    SourceView,
    TimelineEvent,
)
from deps import get_gateway
from fastapi import APIRouter, Depends, Query

router = APIRouter(prefix="/v1", tags=["read"])

GatewayDep = Annotated[Gateway, Depends(get_gateway)]


@router.get("/projects", response_model=list[ProjectView], summary="Project registry + coverage")
def list_projects(
    gateway: GatewayDep,
    project_id: Annotated[list[str] | None, Query(description="Repeatable filter")] = None,
) -> list[ProjectView]:
    return gateway.list_projects(project_id)


@router.get("/projects/{project_id}", response_model=ProjectView, summary="One project")
def get_project(project_id: str, gateway: GatewayDep) -> ProjectView:
    return gateway.get_project(project_id)


@router.get(
    "/entities/{entity_id}",
    response_model=EntityView,
    summary="Entity with the facts current at as_of, its provenance and its neighbours",
)
def get_entity(
    entity_id: UUID,
    gateway: GatewayDep,
    as_of: datetime | None = None,
    include_related: bool = True,
) -> EntityView:
    return gateway.get_entity(entity_id, as_of=as_of, include_related=include_related)


@router.get(
    "/related",
    response_model=RelatedResult,
    summary="1-hop graph expansion (degrades to an empty list with a warning)",
)
def get_related(
    gateway: GatewayDep,
    entity_id: Annotated[list[UUID], Query(description="Repeatable; at least one")],
    as_of: datetime | None = None,
    project_id: Annotated[list[str] | None, Query()] = None,
) -> RelatedResult:
    return gateway.get_related(entity_id, as_of=as_of, project_ids=project_id)


@router.get("/decisions", response_model=list[ArtifactView], summary="Decision artifacts")
def get_decisions(
    gateway: GatewayDep,
    project_id: Annotated[list[str] | None, Query()] = None,
    include_superseded: bool = False,
    as_of: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[ArtifactView]:
    return gateway.get_decisions(
        project_ids=project_id,
        include_superseded=include_superseded,
        as_of=as_of,
        limit=limit,
    )


@router.get("/timeline", response_model=list[TimelineEvent], summary="Merged temporal events")
def get_timeline(
    gateway: GatewayDep,
    project_id: Annotated[list[str] | None, Query()] = None,
    entity_id: Annotated[list[UUID] | None, Query()] = None,
    from_at: Annotated[datetime | None, Query(alias="from")] = None,
    to_at: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[TimelineEvent]:
    return gateway.get_timeline(
        project_ids=project_id,
        entity_ids=entity_id,
        from_at=from_at,
        to_at=to_at,
        limit=limit,
    )


@router.get("/sources", response_model=list[SourceView], summary="Source registry (never bytes)")
def get_sources(
    gateway: GatewayDep,
    project_id: Annotated[list[str] | None, Query()] = None,
    root_id: str | None = None,
    uri: str | None = None,
    include_deleted: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SourceView]:
    return gateway.get_sources(
        project_ids=project_id,
        root_id=root_id,
        uri=uri,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/artifacts/{artifact_id}",
    response_model=ArtifactView,
    summary="Artifact with its supersession chain",
)
def get_artifact(artifact_id: UUID, gateway: GatewayDep) -> ArtifactView:
    return gateway.get_artifact(artifact_id)


@router.get(
    "/state",
    response_model=CurrentState,
    summary="Current state: status, facts, open tasks, decisions, last ingestion, coverage",
)
def get_state(
    gateway: GatewayDep,
    project_id: Annotated[list[str] | None, Query()] = None,
    as_of: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> CurrentState:
    return gateway.get_current_state(project_ids=project_id, as_of=as_of, limit=limit)


@router.get(
    "/explain/{object_id}",
    response_model=ExplainChain,
    summary="Provenance chain: object -> episode -> version -> source -> root/device + models + run",
)
def explain(
    object_id: UUID,
    gateway: GatewayDep,
    object_type: str | None = None,
) -> ExplainChain:
    return gateway.explain(object_id, object_type=object_type)
