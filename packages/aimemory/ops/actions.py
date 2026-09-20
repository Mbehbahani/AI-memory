"""The two writes the Ops page performs (ADR-0011). Both are thin wrappers over
:class:`aimemory.persistence.repositories.MetricsRepo` - the exact insert path the CLI and the
always-on ingestion worker already use, so a request or a verdict created here is indistinguishable
from one created any other way.

Nothing here runs an ingest, calls Ollama, or touches Docker. ``enqueue_run`` only ever inserts a
``run_requests`` row with ``status='queued'``; the worker (A07a, ``aimemory-ingest worker``) is the
only process that executes it (plan section AG point 3: no container gets the Docker socket).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from ..common.ids import new_id
from ..common.time import utc_now
from ..domain.enums import ObjectType, ReviewVerdict, RunAction, RunRequestStatus, Tier
from ..domain.models import ExtractionReview, RunRequest
from ..persistence.repositories import MetricsRepo

__all__ = ["enqueue_run", "record_review"]


def enqueue_run(
    session: Session,
    *,
    action: RunAction,
    root_id: str | None = None,
    tier: Tier = Tier.KNOWLEDGE,
    requested_by: str = "ops-page",
) -> RunRequest:
    """Insert one ``queued`` row. The worker's poll loop picks it up (plan section AG)."""
    request = RunRequest(
        id=new_id(),
        action=action,
        root_id=root_id,
        tier=tier,
        requested_by=requested_by,
        requested_at=utc_now(),
        status=RunRequestStatus.QUEUED,
    )
    return MetricsRepo(session).enqueue_run_request(request)


def record_review(
    session: Session,
    *,
    object_type: ObjectType,
    object_id: UUID,
    verdict: ReviewVerdict,
    reviewer: str = "owner",
    note: str | None = None,
    episode_id: UUID | None = None,
    model_id: str | None = None,
    sample_batch: str | None = None,
) -> ExtractionReview:
    """One human verdict (ADR-0010). Acceptance rate over these rows is the primary quality number."""
    review = ExtractionReview(
        id=new_id(),
        at=utc_now(),
        object_type=object_type,
        object_id=object_id,
        verdict=verdict,
        reviewer=reviewer,
        note=note,
        episode_id=episode_id,
        model_id=model_id,
        sample_batch=sample_batch,
    )
    return MetricsRepo(session).add_review(review)
