"""The two write paths of plan section Q, under the ADR-0008 gate. Owner: A09.

ADR-0008, applied here (the MCP layer adds ``confirm``, a 10/min rate limit and ``mcp_audit_log`` on
top; neither layer can be skipped by going through the other):

* **Off by default.** Both functions refuse with :class:`~aimemory.common.errors.WriteDisabledError`
  (HTTP 403) unless ``GATEWAY_WRITE_ENABLED=true``. Refusal happens *before* anything is read or
  written, and the refusal itself is not recorded as a partial write.
* **Size-limited** to ``MCP_MAX_TEXT_CHARS`` (8,000) - the same number for both entry points, so a
  REST client cannot write something an MCP client could not.
* **Append-only.** A new episode is inserted; a new artifact is inserted; a supersession *link* is
  added to the artifact it replaces. Nothing is ever updated in place and nothing is ever deleted -
  which is what makes "the memory cannot be corrupted silently" true rather than aspirational.
* **Tagged.** ``origin='mcp'`` with ``origin_client`` when a client id is supplied, ``origin='manual'``
  otherwise, so a written-by-an-AI row is always distinguishable from an ingested one.

Manual and MCP episodes have **no source file**, so their provenance carries no ``source_id`` /
``source_version`` - :attr:`Provenance.is_complete` is false for them by design, and the plan
section Y completeness metric is measured over source-derived rows only (see
:mod:`aimemory.provenance.builders`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from ..common.config import Settings, get_settings
from ..common.errors import NotFoundError, SchemaValidationError, WriteDisabledError
from ..common.logging import get_logger
from ..common.time import ensure_utc, utc_now
from ..domain.enums import (
    ArtifactStatus,
    ArtifactType,
    EngineKind,
    EpisodeType,
    ObjectType,
    Origin,
    Tier,
)
from ..domain.models import Episode, KnowledgeArtifact
from ..domain.provenance import DETERMINISTIC_MODEL_ID, Provenance
from ..persistence.repositories import ArtifactRepo, EpisodeRepo
from .models import WriteReceipt

__all__ = ["add_episode", "record_decision", "record_mcp_audit"]

logger = get_logger(__name__)


def _guard(settings: Settings, *, text: str, field: str) -> None:
    """The ADR-0008 gate: the flag first, then the size limit. Both raise, never return a partial."""
    if not settings.gateway.write_enabled:
        raise WriteDisabledError(
            "Writes are disabled (GATEWAY_WRITE_ENABLED=false).",
            context={"flag": "GATEWAY_WRITE_ENABLED"},
        )
    limit = settings.mcp.max_text_chars
    if len(text) > limit:
        raise SchemaValidationError(
            f"{field} exceeds the configured maximum length.",
            context={"field": field, "max_chars": str(limit)},
        )
    if not text.strip():
        raise SchemaValidationError(f"{field} must not be empty.", context={"field": field})


def _project_exists(session: Session, project_id: str | None) -> None:
    if project_id is None:
        return
    from sqlalchemy import text as sql_text

    found = session.execute(
        sql_text("SELECT 1 FROM projects WHERE id = :pid"), {"pid": project_id}
    ).first()
    if found is None:
        raise NotFoundError("No such project.", detail=project_id)


def _manual_provenance(
    *, settings: Settings, project_id: str | None, episode_id: UUID, observed_at: datetime
) -> Provenance:
    """The stamp for a row with no source file. Every column that *can* be known is filled."""
    return Provenance(
        project_id=project_id,
        device_id=settings.device_id,
        observed_at=observed_at,
        valid_from=observed_at,
        confidence=1.0,
        extraction_model_id=DETERMINISTIC_MODEL_ID,
        episode_id=episode_id,
    )


def add_episode(
    session: Session,
    *,
    text: str,
    title: str | None = None,
    project_id: str | None = None,
    client: str | None = None,
    occurred_at: datetime | None = None,
    settings: Settings | None = None,
) -> WriteReceipt:
    """``POST /v1/episodes`` / ``memory.add_episode`` - append one episode. Never extracts inline.

    The episode is queued (``status='pending'``, Tier 2) and the ingestion worker extracts knowledge
    from it later: a write path that also ran an LLM would make an MCP call unbounded in time and
    would put model failures on the write's critical path.
    """
    resolved = settings or get_settings()
    _guard(resolved, text=text, field="text")
    _project_exists(session, project_id)

    now = utc_now()
    episode = Episode(
        id=uuid4(),
        type=EpisodeType.MCP if client else EpisodeType.MANUAL,
        project_id=project_id,
        title=title,
        body=text,
        occurred_at=ensure_utc(occurred_at) if occurred_at else None,
        observed_at=now,
        origin=Origin.EXTERNAL if client else Origin.INTERNAL,
        origin_client=client,
        tier=Tier.KNOWLEDGE,
    )
    EpisodeRepo(session).create(episode)
    logger.info(
        "gateway.episode_added",
        episode_id=str(episode.id),
        project_id=project_id,
        origin=episode.origin.value,
        chars=len(text),
    )
    return WriteReceipt(
        accepted=True,
        object_type=ObjectType.EPISODE,
        object_id=episode.id,
        episode_id=episode.id,
        message="Episode queued for extraction.",
    )


def record_decision(
    session: Session,
    *,
    title: str,
    body: str,
    project_id: str | None = None,
    supersedes_id: UUID | str | None = None,
    client: str | None = None,
    decided_at: datetime | None = None,
    settings: Settings | None = None,
) -> WriteReceipt:
    """``POST /v1/decisions`` / ``memory.record_decision`` - append a decision artifact.

    Two rows and one link, all appends: the episode that records *that* a decision was written, the
    new ``decision`` artifact, and - when ``supersedes_id`` is given - the ADR-0005 rule 6
    supersession link, which marks the old artifact ``superseded`` and closes its validity window
    without deleting a single byte of it.
    """
    resolved = settings or get_settings()
    _guard(resolved, text=body, field="body")
    _guard(resolved, text=title, field="title")
    _project_exists(session, project_id)

    now = utc_now()
    valid_from = ensure_utc(decided_at) if decided_at else now
    episode = Episode(
        id=uuid4(),
        type=EpisodeType.MCP if client else EpisodeType.MANUAL,
        project_id=project_id,
        title=f"decision: {title}",
        body=body,
        occurred_at=valid_from,
        observed_at=now,
        origin=Origin.EXTERNAL if client else Origin.INTERNAL,
        origin_client=client,
        tier=Tier.KNOWLEDGE,
    )
    EpisodeRepo(session).create(episode)

    artifacts = ArtifactRepo(session)
    previous = artifacts.get(UUID(str(supersedes_id))) if supersedes_id else None
    if supersedes_id and previous is None:
        raise NotFoundError("No such artifact to supersede.", detail=str(supersedes_id))

    artifact = KnowledgeArtifact(
        id=uuid4(),
        type=ArtifactType.DECISION,
        title=title,
        body=body,
        project_id=project_id,
        current_status=ArtifactStatus.CURRENT,
        valid_from=valid_from,
        supersedes_id=previous.id if previous else None,
        engine=EngineKind.DETERMINISTIC,
        provenance=_manual_provenance(
            settings=resolved, project_id=project_id, episode_id=episode.id, observed_at=now
        ),
    )
    artifacts.insert(artifact)
    if previous is not None:
        artifacts.supersede(previous.id, artifact.id, valid_from)

    logger.info(
        "gateway.decision_recorded",
        artifact_id=str(artifact.id),
        episode_id=str(episode.id),
        supersedes=str(previous.id) if previous else None,
        project_id=project_id,
    )
    return WriteReceipt(
        accepted=True,
        object_type=ObjectType.ARTIFACT,
        object_id=artifact.id,
        episode_id=episode.id,
        message="Decision recorded." + (" Previous decision superseded." if previous else ""),
    )


def record_mcp_audit(session: Session, payload: Mapping[str, Any]) -> UUID:
    """Persist one ``mcp_audit_log`` row on behalf of the MCP server (ADR-0008).

    **Deliberately not behind ``GATEWAY_WRITE_ENABLED``.** Every other write in this module is gated
    by :func:`_guard`; this one must not be. The rows that matter most are the *refusals* - and a
    refusal only happens while writes are disabled, so gating the audit sink on the write flag would
    guarantee that exactly the records ADR-0008 exists to capture are the ones never stored.

    This is not a knowledge write: it appends to an append-only observability table, touches no
    entity, fact or artifact, and cannot alter memory. The MCP server holds no database credentials
    (the ADR-0008 boundary), so memory-api owning this insert is what keeps that boundary intact.

    Idempotent on ``id``: the MCP server generates the uuid and may retry after a transport failure,
    and a duplicated retry must not double-count a refusal.
    """
    record_id = payload.get("id")
    record_id = UUID(str(record_id)) if record_id else uuid4()
    at = payload.get("at")
    observed = _as_datetime(at) if at else datetime.now(UTC)
    session.execute(
        sql_text(
            """
            INSERT INTO mcp_audit_log
                (id, at, tool, kind, client_id, arguments, confirmed, allowed, denied_reason,
                 result_ref, latency_ms)
            VALUES
                (:id, :at, :tool, :kind, :client_id, :arguments, :confirmed, :allowed,
                 :denied_reason, :result_ref, :latency_ms)
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": record_id,
            "at": observed,
            "tool": str(payload.get("tool") or "unknown"),
            "kind": str(payload.get("kind") or "read"),
            "client_id": payload.get("client_id"),
            "arguments": Jsonb(dict(payload.get("arguments") or {})),
            "confirmed": bool(payload.get("confirmed", False)),
            "allowed": bool(payload.get("allowed", True)),
            "denied_reason": payload.get("denied_reason"),
            "result_ref": payload.get("result_ref"),
            "latency_ms": payload.get("latency_ms"),
        },
    )
    return record_id


def _as_datetime(value: Any) -> datetime:
    """Parse the ISO timestamp the MCP server sends; fall back to now() rather than reject a record."""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.now(UTC)
