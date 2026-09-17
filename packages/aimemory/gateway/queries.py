"""The Gateway's read queries (plan section Q routes other than ``/v1/search``).

Everything is answered from **PostgreSQL** (ADR-0001: the system of record). Neo4j is consulted only
for graph expansion, and only through :mod:`aimemory.retrieval.expansion`, which degrades instead of
failing - so every route in this module keeps working with the graph down.

Three rules run through the file:

* **Never return file bytes.** :func:`list_sources` returns URIs, hashes, sizes and (only when the
  root is registered and enabled) the mapped *container* path. Host paths never appear (ADR-0004).
* **Point-in-time by default.** Anything with a validity window is filtered by the ADR-0005
  predicate at ``as_of`` (default now) *inside the SQL*.
* **Track is attached, not inferred.** :func:`project_tracks` is read once per request and every view
  carries the registry's answer, so the REST layer never has to guess and never merges tracks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.time import ensure_utc, utc_now
from ..domain.enums import ArtifactType, EntityType, ObjectType
from ..domain.provenance import Provenance
from ..retrieval.provenance import DEFAULT_CITATION_FORMAT, load_provenance, render_citation
from ..retrieval.temporal import FactRow
from .models import (
    ArtifactView,
    EntityView,
    FactView,
    IngestionSummary,
    ProjectCoverage,
    ProjectView,
    SourceView,
    TimelineEvent,
)

__all__ = [
    "artifact_views",
    "corpus_counts",
    "entity_view",
    "fact_views",
    "get_artifact",
    "last_ingestion",
    "list_decisions",
    "list_projects",
    "list_sources",
    "open_tasks",
    "project_coverage",
    "project_tracks",
    "timeline",
]

UNKNOWN_TRACK = "unknown"


def _rows(session: Session, sql: str, params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    return [dict(row) for row in session.execute(text(sql), dict(params or {})).mappings()]


def _row(session: Session, sql: str, params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    found = session.execute(text(sql), dict(params or {})).mappings().first()
    return dict(found) if found is not None else None


def project_tracks(session: Session) -> dict[str, str]:
    """``{project_id: track}`` for the whole registry - one small query per request."""
    return {str(row["id"]): str(row["track"]) for row in _rows(session, "SELECT id, track FROM projects")}


def _as_of(value: datetime | None) -> datetime:
    return ensure_utc(value) if value is not None else utc_now()


_COVERAGE_SQL = """
    SELECT
      (SELECT count(*) FROM sources s WHERE s.project_id = :pid)                        AS sources_total,
      (SELECT count(*) FROM sources s WHERE s.project_id = :pid
         AND s.policy IN ('INDEX_CONTENT','MIRROR') AND s.status = 'active')            AS sources_indexable,
      (SELECT count(DISTINCT c.source_id) FROM chunks c
         JOIN embeddings e ON e.text_hash = c.text_hash
        WHERE c.project_id = :pid)                                                      AS sources_embedded,
      (SELECT count(*) FROM chunks c WHERE c.project_id = :pid)                         AS chunks,
      (SELECT count(*) FROM episodes ep WHERE ep.project_id = :pid)                     AS episodes_total,
      (SELECT count(*) FROM episodes ep WHERE ep.project_id = :pid
         AND ep.status = 'extracted')                                                   AS episodes_extracted,
      (SELECT count(*) FROM episodes ep WHERE ep.project_id = :pid
         AND ep.status = 'failed')                                                      AS episodes_failed,
      (SELECT count(*) FROM entities en WHERE en.project_id = :pid)                     AS entities,
      (SELECT count(*) FROM facts f WHERE f.project_id = :pid AND f.valid_to IS NULL)   AS facts,
      (SELECT count(*) FROM knowledge_artifacts a WHERE a.project_id = :pid
         AND a.valid_to IS NULL)                                                        AS artifacts
"""


def _coverage_note(row: Mapping[str, Any]) -> str:
    """A sentence a caller can be shown verbatim. ADR-0006: "not ingested" is a real answer."""
    indexable = int(row["sources_indexable"])
    embedded = int(row["sources_embedded"])
    episodes = int(row["episodes_total"])
    extracted = int(row["episodes_extracted"])
    if indexable == 0 and episodes == 0:
        return "nothing is ingested for this project yet"
    parts = [f"{embedded}/{indexable} indexable sources embedded"]
    if episodes:
        parts.append(f"{extracted}/{episodes} episodes extracted")
    else:
        parts.append("no episodes have been created (Tier 2 has not run)")
    failed = int(row["episodes_failed"])
    if failed:
        parts.append(f"{failed} episodes failed")
    return "; ".join(parts)


def project_coverage(session: Session, project_id: str) -> ProjectCoverage:
    """MEASURED coverage for one project at request time - never cached, never estimated."""
    row = _row(session, _COVERAGE_SQL, {"pid": project_id}) or {}
    indexable = int(row.get("sources_indexable") or 0)
    episodes = int(row.get("episodes_total") or 0)
    embedded = int(row.get("sources_embedded") or 0)
    extracted = int(row.get("episodes_extracted") or 0)
    return ProjectCoverage(
        project_id=project_id,
        sources_total=int(row.get("sources_total") or 0),
        sources_indexable=indexable,
        sources_embedded=embedded,
        chunks=int(row.get("chunks") or 0),
        episodes_total=episodes,
        episodes_extracted=extracted,
        episodes_failed=int(row.get("episodes_failed") or 0),
        entities=int(row.get("entities") or 0),
        facts=int(row.get("facts") or 0),
        artifacts=int(row.get("artifacts") or 0),
        embed_coverage=min(embedded / indexable, 1.0) if indexable else 0.0,
        extraction_coverage=min(extracted / episodes, 1.0) if episodes else 0.0,
        note=_coverage_note(row) if row else "nothing is ingested for this project yet",
    )


_PROJECTS_SQL = """
    SELECT p.id, p.name, p.track, p.status, p.parent_id, p.summary, p.goal_ids, p.root_ids,
           p.attributes, p.created_at, p.updated_at,
           COALESCE(array_agg(pa.alias) FILTER (WHERE pa.alias IS NOT NULL), '{}') AS aliases
      FROM projects p
      LEFT JOIN project_aliases pa ON pa.project_id = p.id
     WHERE (CAST(:project_ids AS text[]) IS NULL OR p.id = ANY(CAST(:project_ids AS text[])))
     GROUP BY p.id
     ORDER BY p.track, p.id
"""


def list_projects(
    session: Session,
    project_ids: Sequence[str] | None = None,
    *,
    with_coverage: bool = True,
) -> list[ProjectView]:
    """The project registry (``GET /v1/projects``), with MEASURED coverage attached by default."""
    rows = _rows(session, _PROJECTS_SQL, {"project_ids": list(project_ids) if project_ids else None})
    views: list[ProjectView] = []
    for row in rows:
        views.append(
            ProjectView(
                id=str(row["id"]),
                name=str(row["name"]),
                track=str(row["track"]),
                status=str(row["status"]),
                parent_id=row.get("parent_id"),
                summary=row.get("summary"),
                goal_ids=list(row.get("goal_ids") or []),
                root_ids=list(row.get("root_ids") or []),
                aliases=sorted(set(row.get("aliases") or [])),
                attributes=dict(row.get("attributes") or {}),
                created_at=row.get("created_at"),
                updated_at=row.get("updated_at"),
                coverage=project_coverage(session, str(row["id"])) if with_coverage else None,
            )
        )
    return views


def fact_views(facts: Sequence[FactRow], tracks: Mapping[str, str]) -> list[FactView]:
    """:class:`FactRow` (stage 5) -> the REST shape, with the citation rendered once."""
    return [
        FactView(
            id=fact.id,
            statement=fact.statement,
            predicate=fact.predicate,
            subject_name=fact.subject_name or None,
            object_name=fact.object_name,
            status=fact.status,
            confidence=fact.confidence,
            project_id=fact.project_id,
            track=tracks.get(fact.project_id or "", UNKNOWN_TRACK),
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            observed_at=fact.observed_at,
            provenance=fact.provenance,
            citation=fact.provenance.citation() if fact.provenance.source_uri else "",
        )
        for fact in facts
    ]


#: ``:PREDICATE:`` is substituted with :data:`_ARTIFACT_SCOPE` or a single-id lookup. Plain
#: ``str.replace`` and not ``str.format``: the SQL contains literal ``'{}'`` array defaults, which
#: ``format`` would read as positional placeholders.
_ARTIFACT_SQL = """
    SELECT a.id, a.type, a.title, a.body, a.structured, a.current_status AS status, a.project_id,
           a.valid_from, a.valid_to, a.confidence, a.evidence_quote, a.supersedes_id,
           a.superseded_by_id, a.source_status,
           prev.title AS supersedes_title, nxt.title AS superseded_by_title,
           COALESCE(array_agg(ae.entity_id) FILTER (WHERE ae.entity_id IS NOT NULL), '{}') AS entity_ids
      FROM knowledge_artifacts a
      LEFT JOIN knowledge_artifacts prev ON prev.id = a.supersedes_id
      LEFT JOIN knowledge_artifacts nxt  ON nxt.id  = a.superseded_by_id
      LEFT JOIN artifact_entities ae     ON ae.artifact_id = a.id
     WHERE :PREDICATE:
     GROUP BY a.id, prev.title, nxt.title
     ORDER BY a.valid_from DESC, a.id
     LIMIT :limit
"""

_ARTIFACT_SCOPE = """
           (CAST(:project_ids AS text[]) IS NULL OR a.project_id = ANY(CAST(:project_ids AS text[])))
       AND (CAST(:types AS text[]) IS NULL OR a.type = ANY(CAST(:types AS text[])))
       -- ADR-0005 §5 only. `current_status` is *today's* state, so testing it here would hide an
       -- artifact that was genuinely current at an earlier `as_of` - the whole point of a
       -- point-in-time read. Supersession is expressed by the closed validity window.
       AND (CAST(:include_superseded AS boolean) OR (
               a.valid_from <= CAST(:as_of AS timestamptz)
               AND (a.valid_to IS NULL OR a.valid_to > CAST(:as_of AS timestamptz))
           ))
       AND a.source_status = ANY(CAST(:allowed_source_status AS text[]))
"""


def artifact_views(
    session: Session,
    rows: Sequence[Mapping[str, Any]],
    tracks: Mapping[str, str],
    *,
    citation_format: str = DEFAULT_CITATION_FORMAT,
    device_id: str = "unknown",
) -> list[ArtifactView]:
    """Attach provenance + citation to artifact rows in one extra query (stage 7 reused)."""
    keys = [(ObjectType.ARTIFACT, UUID(str(row["id"]))) for row in rows]
    stamps: dict[tuple[ObjectType, UUID], Provenance] = (
        load_provenance(session, keys, device_id=device_id) if keys else {}
    )
    views: list[ArtifactView] = []
    for row in rows:
        artifact_id = UUID(str(row["id"]))
        provenance = stamps.get((ObjectType.ARTIFACT, artifact_id))
        views.append(
            ArtifactView(
                id=artifact_id,
                type=ArtifactType(str(row["type"])),
                title=str(row["title"]),
                body=str(row["body"]),
                structured=dict(row.get("structured") or {}),
                status=str(row["status"]),
                project_id=row.get("project_id"),
                track=tracks.get(row.get("project_id") or "", UNKNOWN_TRACK),
                valid_from=row.get("valid_from"),
                valid_to=row.get("valid_to"),
                confidence=float(row.get("confidence") or 1.0),
                evidence_quote=row.get("evidence_quote"),
                supersedes_id=row.get("supersedes_id"),
                supersedes_title=row.get("supersedes_title"),
                superseded_by_id=row.get("superseded_by_id"),
                superseded_by_title=row.get("superseded_by_title"),
                entity_ids=[UUID(str(e)) for e in (row.get("entity_ids") or [])],
                source_status=str(row.get("source_status") or "active"),
                provenance=provenance,
                citation=render_citation(provenance, citation_format) if provenance else "",
            )
        )
    return views


def _artifact_query(
    session: Session,
    *,
    types: Sequence[str] | None,
    project_ids: Sequence[str] | None,
    include_superseded: bool,
    as_of: datetime | None,
    include_deleted_sources: bool,
    limit: int,
) -> list[dict[str, Any]]:
    statuses = ["active", "deleted", "moved"] if include_deleted_sources else ["active"]
    return _rows(
        session,
        _ARTIFACT_SQL.replace(":PREDICATE:", _ARTIFACT_SCOPE),
        {
            "project_ids": list(project_ids) if project_ids else None,
            "types": list(types) if types else None,
            "include_superseded": include_superseded,
            "as_of": _as_of(as_of),
            "allowed_source_status": statuses,
            "limit": limit,
        },
    )


def list_decisions(
    session: Session,
    tracks: Mapping[str, str],
    *,
    project_ids: Sequence[str] | None = None,
    include_superseded: bool = False,
    as_of: datetime | None = None,
    limit: int = 50,
    citation_format: str = DEFAULT_CITATION_FORMAT,
    device_id: str = "unknown",
) -> list[ArtifactView]:
    """Decision artifacts (``GET /v1/decisions``), newest first, superseded ones only on request."""
    rows = _artifact_query(
        session,
        types=[ArtifactType.DECISION.value],
        project_ids=project_ids,
        include_superseded=include_superseded,
        as_of=as_of,
        include_deleted_sources=False,
        limit=limit,
    )
    return artifact_views(
        session, rows, tracks, citation_format=citation_format, device_id=device_id
    )


def open_tasks(
    session: Session,
    tracks: Mapping[str, str],
    *,
    project_ids: Sequence[str] | None = None,
    as_of: datetime | None = None,
    limit: int = 50,
    citation_format: str = DEFAULT_CITATION_FORMAT,
    device_id: str = "unknown",
) -> list[ArtifactView]:
    """Task artifacts that are neither ``done`` nor ``abandoned`` at ``as_of``."""
    rows = [
        row
        for row in _artifact_query(
            session,
            types=[ArtifactType.TASK.value],
            project_ids=project_ids,
            include_superseded=False,
            as_of=as_of,
            include_deleted_sources=False,
            limit=limit * 2,
        )
        if str(row["status"]) not in {"done", "abandoned", "superseded"}
    ][:limit]
    return artifact_views(
        session, rows, tracks, citation_format=citation_format, device_id=device_id
    )


def get_artifact(
    session: Session,
    artifact_id: UUID | str,
    tracks: Mapping[str, str],
    *,
    citation_format: str = DEFAULT_CITATION_FORMAT,
    device_id: str = "unknown",
) -> ArtifactView | None:
    """One artifact plus both ends of its supersession chain (``GET /v1/artifacts/{id}``)."""
    rows = _rows(
        session,
        _ARTIFACT_SQL.replace(":PREDICATE:", "a.id = CAST(:id AS uuid)"),
        {"id": str(artifact_id), "limit": 1},
    )
    if not rows:
        return None
    views = artifact_views(
        session, rows, tracks, citation_format=citation_format, device_id=device_id
    )
    return views[0] if views else None


_ENTITY_SQL = """
    SELECT e.id, e.type, e.canonical_name, e.normalized_name, e.aliases, e.project_id, e.summary,
           e.status, e.confidence, e.engine, e.first_seen_at, e.last_seen_at, e.merged_into_id,
           (SELECT count(*) FROM entity_mentions m WHERE m.entity_id = e.id) AS mention_count
      FROM entities e
     WHERE e.id = CAST(:id AS uuid)
"""

_ENTITY_PROVENANCE_SQL = """
    SELECT p.source_id, p.source_uri, p.source_hash, p.source_version, p.project_id, p.device_id,
           p.observed_at, p.valid_from, p.valid_to, p.confidence, p.extraction_model_id,
           p.embedding_model_id, p.ingestion_run_id, p.episode_id
      FROM provenance_v p
      JOIN entity_mentions m ON m.id = p.object_id
     WHERE p.object_type = 'entity_mention'
       AND m.entity_id = CAST(:id AS uuid)
     ORDER BY p.observed_at DESC
     LIMIT :limit
"""


def entity_view(
    session: Session,
    entity_id: UUID | str,
    tracks: Mapping[str, str],
    *,
    provenance_limit: int = 10,
) -> EntityView | None:
    """An entity with its mention provenance (``GET /v1/entities/{id}``); facts are added by the
    Gateway from stage 5 so the ``as_of`` used is the caller's, not a second default."""
    row = _row(session, _ENTITY_SQL, {"id": str(entity_id)})
    if row is None:
        return None
    stamps = [
        Provenance(**prov)
        for prov in _rows(session, _ENTITY_PROVENANCE_SQL, {"id": str(entity_id), "limit": provenance_limit})
    ]
    return EntityView(
        id=UUID(str(row["id"])),
        type=EntityType(str(row["type"])),
        canonical_name=str(row["canonical_name"]),
        normalized_name=str(row.get("normalized_name") or ""),
        aliases=list(row.get("aliases") or []),
        project_id=row.get("project_id"),
        track=tracks.get(row.get("project_id") or "", UNKNOWN_TRACK),
        summary=row.get("summary"),
        status=row.get("status"),
        confidence=float(row.get("confidence") or 1.0),
        engine=str(row.get("engine") or "deterministic"),
        first_seen_at=row.get("first_seen_at"),
        last_seen_at=row.get("last_seen_at"),
        mention_count=int(row.get("mention_count") or 0),
        provenance=stamps,
    )


_SOURCES_SQL = """
    SELECT s.id, s.uri, s.root_id, s.relative_path, s.project_id, s.kind, s.media_type, s.policy,
           s.policy_reason, s.status, s.origin, s.trust, s.secret_suspected, s.current_version_id,
           s.last_seen_at, sv.content_hash, sv.size_bytes, sv.observed_at,
           sr.container_path, sr.enabled AS root_enabled,
           (SELECT count(*) FROM source_versions v WHERE v.source_id = s.id)  AS versions,
           (SELECT count(*) FROM chunks c WHERE c.source_id = s.id)           AS chunks
      FROM sources s
      LEFT JOIN source_versions sv ON sv.id = s.current_version_id
      LEFT JOIN source_roots sr    ON sr.root_id = s.root_id
     WHERE (CAST(:project_ids AS text[]) IS NULL OR s.project_id = ANY(CAST(:project_ids AS text[])))
       AND (CAST(:root_id AS text) IS NULL OR s.root_id = CAST(:root_id AS text))
       AND s.status = ANY(CAST(:statuses AS text[]))
       AND (CAST(:uri AS text) IS NULL OR s.uri = CAST(:uri AS text))
       AND (CAST(:include_secret AS boolean) OR s.secret_suspected = false)
     ORDER BY s.uri
     LIMIT :limit OFFSET :offset
"""


def list_sources(
    session: Session,
    tracks: Mapping[str, str],
    *,
    project_ids: Sequence[str] | None = None,
    root_id: str | None = None,
    uri: str | None = None,
    statuses: Sequence[str] = ("active",),
    include_secret_suspected: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[SourceView]:
    """The source registry - URIs, hashes and sizes. **Never** file bytes (plan section Q).

    ``container_path`` is filled only when the root row is enabled; a disabled root means the mount
    is not there, and returning a path that resolves to nothing would be a lie rather than a hint.
    Secret-suspected sources are hidden unless explicitly asked for (plan section T).
    """
    rows = _rows(
        session,
        _SOURCES_SQL,
        {
            "project_ids": list(project_ids) if project_ids else None,
            "root_id": root_id,
            "uri": uri,
            "statuses": list(statuses),
            "include_secret": include_secret_suspected,
            "limit": limit,
            "offset": offset,
        },
    )
    views: list[SourceView] = []
    for row in rows:
        container = row.get("container_path")
        mounted = bool(row.get("root_enabled")) and bool(container)
        views.append(
            SourceView(
                id=UUID(str(row["id"])),
                uri=str(row["uri"]),
                root_id=str(row["root_id"]),
                relative_path=str(row["relative_path"]),
                project_id=row.get("project_id"),
                track=tracks.get(row.get("project_id") or "", UNKNOWN_TRACK),
                kind=str(row.get("kind") or "file"),
                media_type=row.get("media_type"),
                policy=str(row.get("policy") or "CATALOG_ONLY"),
                policy_reason=row.get("policy_reason"),
                status=str(row.get("status") or "active"),
                origin=str(row.get("origin") or "internal"),
                trust=str(row.get("trust") or "high"),
                secret_suspected=bool(row.get("secret_suspected")),
                container_path=f"{container}/{row['relative_path']}" if mounted else None,
                current_version_id=row.get("current_version_id"),
                content_hash=row.get("content_hash"),
                size_bytes=row.get("size_bytes"),
                observed_at=row.get("observed_at"),
                last_seen_at=row.get("last_seen_at"),
                versions=int(row.get("versions") or 0),
                chunks=int(row.get("chunks") or 0),
            )
        )
    return views


#: ``temporal.md`` §8: facts (``valid_from``, and ``valid_to`` as a second "closed" event),
#: artifacts (``valid_from`` / supersession) and ``source_events``, merged and ordered.
_TIMELINE_SQL = """
    SELECT at, kind, object_type, object_id, title, project_id, detail FROM (
        SELECT f.valid_from AS at, 'fact_opened' AS kind, 'fact' AS object_type, f.id AS object_id,
               f.statement AS title, f.project_id,
               jsonb_build_object('predicate', f.predicate, 'status', f.status) AS detail
          FROM facts f
         WHERE (CAST(:entity_ids AS uuid[]) IS NULL
                OR f.subject_entity_id = ANY(CAST(:entity_ids AS uuid[]))
                OR f.object_entity_id  = ANY(CAST(:entity_ids AS uuid[])))
        UNION ALL
        SELECT f.valid_to, 'fact_closed', 'fact', f.id, f.statement, f.project_id,
               jsonb_build_object('predicate', f.predicate, 'status', f.status)
          FROM facts f
         WHERE f.valid_to IS NOT NULL
           AND (CAST(:entity_ids AS uuid[]) IS NULL
                OR f.subject_entity_id = ANY(CAST(:entity_ids AS uuid[]))
                OR f.object_entity_id  = ANY(CAST(:entity_ids AS uuid[])))
        UNION ALL
        SELECT a.valid_from, 'artifact', 'artifact', a.id, a.title, a.project_id,
               jsonb_build_object('type', a.type, 'status', a.current_status,
                                  'supersedes', COALESCE(CAST(a.supersedes_id AS text), ''))
          FROM knowledge_artifacts a
         WHERE CAST(:entity_ids AS uuid[]) IS NULL
            OR EXISTS (SELECT 1 FROM artifact_entities ae
                        WHERE ae.artifact_id = a.id
                          AND ae.entity_id = ANY(CAST(:entity_ids AS uuid[])))
        UNION ALL
        SELECT se.at, 'source_event', 'source', se.source_id, s.uri, s.project_id,
               jsonb_build_object('event_type', se.event_type)
          FROM source_events se
          JOIN sources s ON s.id = se.source_id
         WHERE CAST(:entity_ids AS uuid[]) IS NULL
    ) events
     WHERE at IS NOT NULL
       AND (CAST(:project_ids AS text[]) IS NULL OR project_id = ANY(CAST(:project_ids AS text[])))
       AND (CAST(:from_at AS timestamptz) IS NULL OR at >= CAST(:from_at AS timestamptz))
       AND (CAST(:to_at   AS timestamptz) IS NULL OR at <= CAST(:to_at   AS timestamptz))
     ORDER BY at DESC, object_id
     LIMIT :limit
"""


def timeline(
    session: Session,
    tracks: Mapping[str, str],
    *,
    project_ids: Sequence[str] | None = None,
    entity_ids: Sequence[UUID] | None = None,
    from_at: datetime | None = None,
    to_at: datetime | None = None,
    limit: int = 100,
) -> list[TimelineEvent]:
    """The merged temporal stream (``GET /v1/timeline``), newest first."""
    rows = _rows(
        session,
        _TIMELINE_SQL,
        {
            "project_ids": list(project_ids) if project_ids else None,
            "entity_ids": [str(e) for e in entity_ids] if entity_ids else None,
            "from_at": ensure_utc(from_at) if from_at else None,
            "to_at": ensure_utc(to_at) if to_at else None,
            "limit": limit,
        },
    )
    events: list[TimelineEvent] = []
    for row in rows:
        raw_type = row.get("object_type")
        events.append(
            TimelineEvent(
                at=row["at"],
                kind=str(row["kind"]),
                object_type=ObjectType(str(raw_type)) if raw_type else None,
                object_id=UUID(str(row["object_id"])) if row.get("object_id") else None,
                title=str(row.get("title") or ""),
                project_id=row.get("project_id"),
                track=tracks.get(row.get("project_id") or "", UNKNOWN_TRACK),
                detail={k: str(v) for k, v in (row.get("detail") or {}).items() if v is not None},
            )
        )
    return events


_LAST_RUN_SQL = """
    SELECT r.id, r.root_id, r.tier, r.trigger, r.status, r.started_at, r.finished_at, r.counters,
           r.error
      FROM ingestion_runs r
     WHERE (CAST(:root_id AS text) IS NULL OR r.root_id = CAST(:root_id AS text))
     ORDER BY r.started_at DESC
     LIMIT 1
"""


def last_ingestion(session: Session, *, root_id: str | None = None) -> IngestionSummary | None:
    """The most recent ingestion run - ``None`` when nothing has ever been ingested.

    ``None`` is an honest answer the caller must be able to distinguish from "ingested but empty";
    ADR-0006 makes "the memory has not learned this yet" a normal state.
    """
    row = _row(session, _LAST_RUN_SQL, {"root_id": root_id})
    if row is None:
        return None
    return IngestionSummary(
        run_id=UUID(str(row["id"])),
        root_id=row.get("root_id"),
        tier=int(row["tier"]) if row.get("tier") is not None else None,
        trigger=row.get("trigger"),
        status=row.get("status"),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        counters=dict(row.get("counters") or {}),
        error=row.get("error"),
    )


_CORPUS_SQL = """
    SELECT
      (SELECT count(*) FROM projects)                                   AS projects,
      (SELECT count(*) FROM sources)                                    AS sources,
      (SELECT count(*) FROM sources WHERE status = 'active')            AS sources_active,
      (SELECT count(*) FROM source_versions)                            AS source_versions,
      (SELECT count(*) FROM chunks)                                     AS chunks,
      (SELECT count(*) FROM embeddings)                                 AS embeddings,
      (SELECT count(*) FROM episodes)                                   AS episodes,
      (SELECT count(*) FROM episodes WHERE status = 'extracted')        AS episodes_extracted,
      (SELECT count(*) FROM entities)                                   AS entities,
      (SELECT count(*) FROM facts WHERE valid_to IS NULL)               AS facts_current,
      (SELECT count(*) FROM knowledge_artifacts WHERE valid_to IS NULL) AS artifacts_current,
      (SELECT count(*) FROM retrieval_logs)                             AS retrieval_logs,
      (SELECT count(*) FROM ingestion_runs)                             AS ingestion_runs
"""


def corpus_counts(session: Session) -> dict[str, int]:
    """MEASURED corpus totals at request time - the numbers ``/metrics`` reports."""
    row = _row(session, _CORPUS_SQL) or {}
    return {key: int(value or 0) for key, value in row.items()}
