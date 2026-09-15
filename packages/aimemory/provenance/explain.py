"""``explain(object_id)`` - the provenance chain resolver the Gateway serves (plan section J, A08).

Plan section J, verbatim: *"``Gateway.explain(id)`` returns the chain artifact/fact -> episode ->
version -> source -> root/device + models + run."* That is exactly the order
:func:`explain` emits, as :class:`~aimemory.domain.provenance.ProvenanceChainStep` hops.

Everything is answered from **PostgreSQL only** (ADR-0001). Neo4j is never consulted: a chain that
needed the projection to be intact would not survive ``rebuild-graph``, and would make the graph the
system of record by the back door.

The object type is discovered, not demanded: ``facts``, ``knowledge_artifacts`` and
``entity_mentions`` are probed in that order, so the Gateway can expose one ``/v1/explain/{id}``
route. ``entities`` is probed last and answers a reduced chain (an entity has no ``[PROV]`` stamp of
its own - its evidence is its mentions), which is why :attr:`ExplainResult.complete` is defined
against the object's own stamp rather than against the number of hops.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.errors import NotFoundError
from ..domain.base import DomainModel
from ..domain.provenance import Provenance, ProvenanceChainStep
from .builders import missing_provenance_columns, provenance_payload

__all__ = ["ExplainResult", "explain", "explain_many", "provenance_completeness"]

#: ``object_type`` -> table, probed in this order by :func:`explain`.
_OBJECT_TABLES: dict[str, str] = {
    "fact": "facts",
    "artifact": "knowledge_artifacts",
    "entity_mention": "entity_mentions",
}

_PROV_SELECT = """
    SELECT source_id, source_uri, source_hash, source_version, project_id, device_id,
           observed_at, valid_from, valid_to, confidence, extraction_model_id,
           embedding_model_id, ingestion_run_id, episode_id
      FROM {table}
     WHERE id = :id
"""


class ExplainResult(DomainModel):
    """One resolved chain. ``steps`` is ordered; ``provenance`` is the object's own ``[PROV]`` row."""

    object_type: str
    object_id: str
    label: str = ""
    provenance: Provenance | None = None
    steps: list[ProvenanceChainStep] = Field(default_factory=list)
    complete: bool = False
    missing: list[str] = Field(default_factory=list)

    def as_payload(self) -> dict[str, Any]:
        """Serialization used by the Gateway and MCP: the stamp in ``PROVENANCE_COLUMNS`` order."""
        return {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "label": self.label,
            "complete": self.complete,
            "missing": list(self.missing),
            "provenance": provenance_payload(self.provenance) if self.provenance else None,
            "chain": [step.model_dump() for step in self.steps],
        }


def _row(session: Session, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
    result = session.execute(text(sql), params).mappings().first()
    return dict(result) if result is not None else None


def _detect(session: Session, object_id: UUID | str) -> tuple[str, dict[str, Any]] | None:
    for object_type, table in _OBJECT_TABLES.items():
        row = _row(session, _PROV_SELECT.format(table=table), {"id": str(object_id)})
        if row is not None:
            return object_type, row
    return None


def _label_for(session: Session, object_type: str, object_id: UUID | str) -> str:
    sql = {
        "fact": "SELECT statement AS label FROM facts WHERE id = :id",
        "artifact": "SELECT title AS label FROM knowledge_artifacts WHERE id = :id",
        "entity_mention": "SELECT surface_form AS label FROM entity_mentions WHERE id = :id",
        "entity": "SELECT canonical_name AS label FROM entities WHERE id = :id",
    }[object_type]
    row = _row(session, sql, {"id": str(object_id)})
    return str(row["label"]) if row else ""


def _entity_chain(session: Session, object_id: UUID | str) -> ExplainResult | None:
    """An entity has no stamp of its own; its chain is its most recent mention's."""
    row = _row(
        session,
        "SELECT id FROM entity_mentions WHERE entity_id = :id ORDER BY observed_at DESC LIMIT 1",
        {"id": str(object_id)},
    )
    if row is None:
        return None
    nested = explain(session, row["id"], object_type="entity_mention")
    name = _label_for(session, "entity", object_id)
    steps = [
        ProvenanceChainStep(
            kind="entity",
            id=str(object_id),
            label=name,
            detail={"via": "most recent entity_mention"},
        ),
        *nested.steps,
    ]
    return ExplainResult(
        object_type="entity",
        object_id=str(object_id),
        label=name,
        provenance=nested.provenance,
        steps=steps,
        complete=nested.complete,
        missing=list(nested.missing),
    )


def explain(
    session: Session,
    object_id: UUID | str,
    *,
    object_type: str | None = None,
) -> ExplainResult:
    """Resolve the full provenance chain for one derived object.

    Raises :class:`~aimemory.common.errors.NotFoundError` when nothing with this id exists in any of
    the four tables - the Gateway turns that into a 404 rather than an empty chain, because "no
    provenance" and "no such object" are different answers.
    """
    oid = str(object_id)

    if object_type == "entity" or (object_type is None and _object_is_entity(session, oid)):
        result = _entity_chain(session, oid)
        if result is not None:
            return result
        name = _label_for(session, "entity", oid)
        return ExplainResult(
            object_type="entity",
            object_id=oid,
            label=name,
            steps=[ProvenanceChainStep(kind="entity", id=oid, label=name)],
            complete=False,
            missing=["episode_id"],
        )

    if object_type is not None:
        table = _OBJECT_TABLES.get(object_type)
        if table is None:
            raise NotFoundError(f"Unknown provenance object type {object_type!r}.")
        row = _row(session, _PROV_SELECT.format(table=table), {"id": oid})
        if row is None:
            raise NotFoundError("No such object.", detail=f"{object_type}:{oid}")
        detected = (object_type, row)
    else:
        found = _detect(session, oid)
        if found is None:
            raise NotFoundError("No such object.", detail=oid)
        detected = found

    kind, prov_row = detected
    provenance = Provenance(**prov_row)
    steps: list[ProvenanceChainStep] = [
        ProvenanceChainStep(
            kind=kind,
            id=oid,
            label=_label_for(session, kind, oid),
            detail=_str_detail(
                {
                    "valid_from": provenance.valid_from,
                    "valid_to": provenance.valid_to,
                    "confidence": provenance.confidence,
                }
            ),
        )
    ]
    steps.extend(_context_steps(session, provenance))
    missing = missing_provenance_columns(provenance)
    return ExplainResult(
        object_type=kind,
        object_id=oid,
        label=steps[0].label,
        provenance=provenance,
        steps=steps,
        complete=not missing,
        missing=missing,
    )


def _object_is_entity(session: Session, oid: str) -> bool:
    return _row(session, "SELECT 1 AS hit FROM entities WHERE id = :id", {"id": oid}) is not None


def _str_detail(values: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in values.items() if v is not None}


def _context_steps(session: Session, provenance: Provenance) -> list[ProvenanceChainStep]:
    """episode -> version -> source -> root -> device, then models and the run."""
    steps: list[ProvenanceChainStep] = []

    if provenance.episode_id is not None:
        row = _row(
            session,
            "SELECT id, type, title, observed_at, engine, status FROM episodes WHERE id = :id",
            {"id": str(provenance.episode_id)},
        )
        if row is not None:
            steps.append(
                ProvenanceChainStep(
                    kind="episode",
                    id=str(row["id"]),
                    label=str(row["title"] or row["type"]),
                    detail=_str_detail(
                        {
                            "type": row["type"],
                            "observed_at": row["observed_at"],
                            "engine": row["engine"],
                            "status": row["status"],
                        }
                    ),
                )
            )

    if provenance.source_version is not None:
        row = _row(
            session,
            "SELECT id, content_hash, size_bytes, mtime, change_type, is_current, git_commit "
            "FROM source_versions WHERE id = :id",
            {"id": str(provenance.source_version)},
        )
        if row is not None:
            steps.append(
                ProvenanceChainStep(
                    kind="version",
                    id=str(row["id"]),
                    label=str(row["content_hash"]),
                    detail=_str_detail(
                        {
                            "size_bytes": row["size_bytes"],
                            "mtime": row["mtime"],
                            "change_type": row["change_type"],
                            "is_current": row["is_current"],
                            "git_commit": row["git_commit"],
                        }
                    ),
                )
            )

    root_id: str | None = None
    if provenance.source_id is not None:
        row = _row(
            session,
            "SELECT id, uri, root_id, relative_path, status, policy, origin, trust, "
            "secret_suspected FROM sources WHERE id = :id",
            {"id": str(provenance.source_id)},
        )
        if row is not None:
            root_id = str(row["root_id"])
            steps.append(
                ProvenanceChainStep(
                    kind="source",
                    id=str(row["id"]),
                    label=str(row["uri"]),
                    detail=_str_detail(
                        {
                            "relative_path": row["relative_path"],
                            "status": row["status"],
                            "policy": row["policy"],
                            "origin": row["origin"],
                            "trust": row["trust"],
                            "secret_suspected": row["secret_suspected"],
                        }
                    ),
                )
            )

    device_id: str | None = provenance.device_id
    if root_id is not None:
        row = _row(
            session,
            "SELECT root_id, label, scheme, device_id, container_path FROM source_roots "
            "WHERE root_id = :id",
            {"id": root_id},
        )
        if row is not None:
            device_id = str(row["device_id"])
            steps.append(
                ProvenanceChainStep(
                    kind="root",
                    id=str(row["root_id"]),
                    label=str(row["label"]),
                    detail=_str_detail(
                        {"scheme": row["scheme"], "container_path": row["container_path"]}
                    ),
                )
            )

    if device_id:
        row = _row(session, "SELECT id, label, os FROM devices WHERE id = :id", {"id": device_id})
        steps.append(
            ProvenanceChainStep(
                kind="device",
                id=device_id,
                label=str(row["label"]) if row else device_id,
                detail=_str_detail({"os": row["os"]}) if row else {},
            )
        )

    if provenance.extraction_model_id:
        row = _row(
            session,
            "SELECT id, provider, name, digest FROM extraction_models WHERE id = :id",
            {"id": provenance.extraction_model_id},
        )
        steps.append(
            ProvenanceChainStep(
                kind="model",
                id=provenance.extraction_model_id,
                label=str(row["name"]) if row else provenance.extraction_model_id,
                detail=_str_detail(
                    {"role": "extraction", "provider": row["provider"], "digest": row["digest"]}
                    if row
                    else {"role": "extraction"}
                ),
            )
        )

    if provenance.embedding_model_id:
        row = _row(
            session,
            "SELECT id, name, dimension, revision FROM embedding_models WHERE id = :id",
            {"id": provenance.embedding_model_id},
        )
        steps.append(
            ProvenanceChainStep(
                kind="model",
                id=provenance.embedding_model_id,
                label=str(row["name"]) if row else provenance.embedding_model_id,
                detail=_str_detail(
                    {"role": "embedding", "dimension": row["dimension"], "revision": row["revision"]}
                    if row
                    else {"role": "embedding"}
                ),
            )
        )

    if provenance.ingestion_run_id is not None:
        row = _row(
            session,
            "SELECT id, root_id, tier, trigger, status, started_at, finished_at "
            "FROM ingestion_runs WHERE id = :id",
            {"id": str(provenance.ingestion_run_id)},
        )
        if row is not None:
            steps.append(
                ProvenanceChainStep(
                    kind="run",
                    id=str(row["id"]),
                    label=f"{row['tier']} run ({row['trigger']})",
                    detail=_str_detail(
                        {
                            "root_id": row["root_id"],
                            "status": row["status"],
                            "started_at": row["started_at"],
                            "finished_at": row["finished_at"],
                        }
                    ),
                )
            )

    return steps


def explain_many(
    session: Session, object_ids: Sequence[UUID | str], *, object_type: str | None = None
) -> list[ExplainResult]:
    """:func:`explain` over a list, skipping ids that do not exist."""
    results: list[ExplainResult] = []
    for object_id in object_ids:
        try:
            results.append(explain(session, object_id, object_type=object_type))
        except NotFoundError:
            continue
    return results


def provenance_completeness(
    session: Session, *, episode_id: UUID | str | None = None, run_id: UUID | str | None = None
) -> dict[str, Any]:
    """The plan section Y metric: share of source-derived rows whose chain reaches a version.

    Counted over ``provenance_v`` so facts, artifacts and mentions are measured the same way.
    Rows with no ``source_id`` at all (manual / MCP episodes) are excluded from the denominator,
    which is what :attr:`Provenance.is_complete` documents.
    """
    where = ["source_id IS NOT NULL"]
    params: dict[str, Any] = {}
    if episode_id is not None:
        where.append("episode_id = :episode_id")
        params["episode_id"] = str(episode_id)
    if run_id is not None:
        where.append("ingestion_run_id = :run_id")
        params["run_id"] = str(run_id)
    clause = " AND ".join(where)
    row = _row(
        session,
        f"""
        SELECT count(*) AS total,
               count(*) FILTER (
                   WHERE source_version IS NOT NULL AND episode_id IS NOT NULL
                     AND extraction_model_id IS NOT NULL AND source_hash IS NOT NULL
               ) AS complete
          FROM provenance_v
         WHERE {clause}
        """,
        params,
    )
    total = int(row["total"]) if row else 0
    complete = int(row["complete"]) if row else 0
    return {
        "total": total,
        "complete": complete,
        "incomplete": total - complete,
        "ratio": (complete / total) if total else 1.0,
    }
