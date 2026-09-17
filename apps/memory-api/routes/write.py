"""The two ADR-0008 write routes. Owner: A09.

Both are **off unless ``GATEWAY_WRITE_ENABLED=true``** and both are append-only. The refusal is a
403 produced by :class:`~aimemory.common.errors.WriteDisabledError` inside the Gateway, not by a
check in this module - so the MCP server, which calls the same service functions through this API,
cannot reach a write path that skipped the gate.

The request bodies cap text at :data:`MAX_TEXT_CHARS` so an oversized payload is rejected as a 422
before any work happens; the Gateway re-checks the same limit, because a second entry point must not
be able to exceed what the first one allows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from aimemory.common.config import get_settings
from aimemory.domain.base import DomainModel
from aimemory.gateway import Gateway, WriteReceipt
from deps import get_gateway
from fastapi import APIRouter, Depends
from pydantic import Field

router = APIRouter(prefix="/v1", tags=["write"])

#: ADR-0008 size limit, read once at import so the OpenAPI document states the real number.
MAX_TEXT_CHARS = get_settings().mcp.max_text_chars

GatewayDep = Annotated[Gateway, Depends(get_gateway)]


class EpisodeCreate(DomainModel):
    """``POST /v1/episodes`` body. ``client`` tags the writer (ADR-0008 audit requirement)."""

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    title: str | None = Field(default=None, max_length=500)
    project_id: str | None = None
    client: str | None = Field(default=None, description="MCP client id; omitted for a manual write")
    occurred_at: datetime | None = None


class DecisionCreate(DomainModel):
    """``POST /v1/decisions`` body. ``supersedes_id`` adds the ADR-0005 rule 6 link, never a delete."""

    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    project_id: str | None = None
    supersedes_id: UUID | None = None
    client: str | None = None
    decided_at: datetime | None = None


@router.post(
    "/episodes",
    response_model=WriteReceipt,
    status_code=201,
    summary="Append an episode (403 unless GATEWAY_WRITE_ENABLED)",
)
def add_episode(payload: EpisodeCreate, gateway: GatewayDep) -> WriteReceipt:
    return gateway.add_episode(
        text=payload.text,
        title=payload.title,
        project_id=payload.project_id,
        client=payload.client,
        occurred_at=payload.occurred_at,
    )


@router.post(
    "/decisions",
    response_model=WriteReceipt,
    status_code=201,
    summary="Append a decision artifact (403 unless GATEWAY_WRITE_ENABLED)",
)
def record_decision(payload: DecisionCreate, gateway: GatewayDep) -> WriteReceipt:
    return gateway.record_decision(
        title=payload.title,
        body=payload.body,
        project_id=payload.project_id,
        supersedes_id=payload.supersedes_id,
        client=payload.client,
        decided_at=payload.decided_at,
    )
