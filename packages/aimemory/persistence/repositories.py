"""Repositories - the only place raw SQL lives (P5-T01 task brief).

Every repository takes a live :class:`~sqlalchemy.orm.Session` (never opens its own transaction) so
callers control the transaction boundary and tests can pass a session bound to a savepoint that
always rolls back. Every method returns the pydantic models of :mod:`aimemory.domain.models` -
never a raw row, never an ORM object (``docs/architecture/data-model.md`` §7).

Idempotency rules implemented here (data-model.md §7):

1. ``INSERT ... ON CONFLICT (version_id, stage) DO UPDATE`` for jobs (:meth:`JobRepo.upsert`).
2. ``INSERT ... ON CONFLICT (source_id, content_hash) DO NOTHING RETURNING id`` for versions
   (:meth:`SourceRepo.record_version`).
3. ``INSERT ... ON CONFLICT (text_hash, model_id) DO NOTHING`` for embeddings
   (:meth:`EmbeddingRepo.bulk_insert`).
4. Deterministic ids for chunks/structural entities are the *caller's* responsibility
   (:func:`aimemory.common.ids.deterministic_id`); the chunk/entity upserts here are idempotent on
   whatever id they are given.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..domain.enums import RunStatus
from ..domain.models import (
    ArtifactEntity,
    Chunk,
    Embedding,
    Entity,
    EntityMention,
    Episode,
    ExtractionReview,
    Fact,
    IngestionJob,
    IngestionRun,
    KnowledgeArtifact,
    McpAuditLog,
    MetricsSnapshot,
    Project,
    ProjectAlias,
    RetrievalLog,
    RunRequest,
    ServiceStat,
    Source,
    SourceEvent,
    SourceText,
    SourceVersion,
)
from ..domain.provenance import Provenance

__all__ = [
    "ArtifactRepo",
    "AuditRepo",
    "ChunkRepo",
    "EmbeddingRepo",
    "EntityRepo",
    "EpisodeRepo",
    "FactRepo",
    "JobRepo",
    "MetricsRepo",
    "ProjectRepo",
    "RunRepo",
    "SourceRepo",
]


def _m(row: Any) -> dict[str, Any]:
    """A plain ``dict`` from a SQLAlchemy result row (or ``{}`` for no row)."""
    return dict(row._mapping) if row is not None else {}


def _vector_to_list(value: Any) -> list[float]:
    """``pgvector.psycopg.register_vector`` (wired up in ``db.py``) returns a ``pgvector.Vector``
    wrapper, not a plain list; ``Embedding.vector`` is typed ``list[float]`` (ports.py)."""
    if hasattr(value, "to_list"):
        return list(value.to_list())
    return list(value)


def _wrap_jsonb(params: dict[str, Any], *keys: str) -> dict[str, Any]:
    """psycopg3 has no default adapter for a bare ``dict``/``list`` bound to a ``jsonb`` column
    (it would not know whether to encode ``json`` or ``jsonb``, or a Python list as an array or as
    JSON) - wrap the value so it serializes unambiguously. Mutates and returns ``params``."""
    for key in keys:
        if key in params and params[key] is not None:
            params[key] = Jsonb(params[key])
    return params


def _prov_dict(p: Provenance) -> dict[str, Any]:
    return {
        "source_id": p.source_id,
        "source_uri": p.source_uri,
        "source_hash": p.source_hash,
        "source_version": p.source_version,
        "project_id": p.project_id,
        "device_id": p.device_id,
        "observed_at": p.observed_at,
        "valid_from": p.valid_from,
        "valid_to": p.valid_to,
        "confidence": p.confidence,
        "extraction_model_id": p.extraction_model_id,
        "embedding_model_id": p.embedding_model_id,
        "ingestion_run_id": p.ingestion_run_id,
        "episode_id": p.episode_id,
    }


def _provenance_from_row(
    m: Mapping[str, Any], *, confidence_key: str = "confidence", episode_key: str = "episode_id"
) -> Provenance:
    return Provenance(
        source_id=m.get("source_id"),
        source_uri=m.get("source_uri"),
        source_hash=m.get("source_hash"),
        source_version=m.get("source_version"),
        project_id=m.get("project_id"),
        device_id=m["device_id"],
        observed_at=m["observed_at"],
        valid_from=m.get("valid_from"),
        valid_to=m.get("valid_to"),
        confidence=m.get(confidence_key, 1.0),
        extraction_model_id=m.get("extraction_model_id"),
        embedding_model_id=m.get("embedding_model_id"),
        ingestion_run_id=m.get("ingestion_run_id"),
        episode_id=m.get(episode_key),
        heading_path=list(m.get("heading_path") or []),
        char_start=m.get("char_start"),
        char_end=m.get("char_end"),
    )


# ------------------------------------------------------------------------------------------------
# Registries
# ------------------------------------------------------------------------------------------------


class ProjectRepo:
    """``projects`` / ``project_aliases``."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(self, project: Project) -> Project:
        row = self._s.execute(
            text(
                """
                INSERT INTO projects (id, name, track, parent_id, status, goal_ids, summary,
                                       attributes, root_ids, created_at, updated_at)
                VALUES (:id, :name, :track, :parent_id, :status, :goal_ids, :summary,
                        :attributes, :root_ids, :created_at, :updated_at)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name, track = EXCLUDED.track, parent_id = EXCLUDED.parent_id,
                    status = EXCLUDED.status, goal_ids = EXCLUDED.goal_ids,
                    summary = EXCLUDED.summary, attributes = EXCLUDED.attributes,
                    root_ids = EXCLUDED.root_ids, updated_at = EXCLUDED.updated_at
                RETURNING *
                """
            ),
            _wrap_jsonb(
                {
                    "id": project.id,
                    "name": project.name,
                    "track": project.track.value,
                    "parent_id": project.parent_id,
                    "status": project.status.value,
                    "goal_ids": project.goal_ids,
                    "summary": project.summary,
                    "attributes": project.attributes,
                    "root_ids": project.root_ids,
                    "created_at": project.created_at,
                    "updated_at": project.updated_at,
                },
                "attributes",
            ),
        ).first()
        return Project(**_m(row))

    def get(self, project_id: str) -> Project | None:
        row = self._s.execute(
            text("SELECT * FROM projects WHERE id = :id"), {"id": project_id}
        ).first()
        return Project(**_m(row)) if row is not None else None

    def add_alias(self, alias: ProjectAlias) -> ProjectAlias:
        row = self._s.execute(
            text(
                """
                INSERT INTO project_aliases (id, project_id, alias, normalized_alias, source)
                VALUES (:id, :project_id, :alias, :normalized_alias, :source)
                ON CONFLICT (normalized_alias) DO NOTHING
                RETURNING *
                """
            ),
            alias.model_dump(),
        ).first()
        if row is None:
            existing = self._s.execute(
                text("SELECT * FROM project_aliases WHERE normalized_alias = :n"),
                {"n": alias.normalized_alias},
            ).first()
            return ProjectAlias(**_m(existing))
        return ProjectAlias(**_m(row))


# ------------------------------------------------------------------------------------------------
# Sources and content
# ------------------------------------------------------------------------------------------------


class SourceRepo:
    """``sources`` / ``source_versions`` / ``source_text`` (plan section L)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert_source(self, source: Source) -> Source:
        """Insert or refresh a source by its unique logical URI (ADR-0004)."""
        row = self._s.execute(
            text(
                """
                INSERT INTO sources (id, uri, root_id, relative_path, project_id, kind,
                                      media_type, policy, policy_reason, status, moved_from_uri,
                                      current_version_id, secret_suspected, origin, trust,
                                      first_seen_at, last_seen_at)
                VALUES (:id, :uri, :root_id, :relative_path, :project_id, :kind, :media_type,
                        :policy, :policy_reason, :status, :moved_from_uri, :current_version_id,
                        :secret_suspected, :origin, :trust, :first_seen_at, :last_seen_at)
                ON CONFLICT (uri) DO UPDATE SET
                    project_id = EXCLUDED.project_id, kind = EXCLUDED.kind,
                    media_type = EXCLUDED.media_type, policy = EXCLUDED.policy,
                    policy_reason = EXCLUDED.policy_reason, status = EXCLUDED.status,
                    secret_suspected = EXCLUDED.secret_suspected, origin = EXCLUDED.origin,
                    trust = EXCLUDED.trust, last_seen_at = EXCLUDED.last_seen_at
                RETURNING *
                """
            ),
            {
                "id": source.id,
                "uri": source.uri,
                "root_id": source.root_id,
                "relative_path": source.relative_path,
                "project_id": source.project_id,
                "kind": source.kind.value,
                "media_type": source.media_type,
                "policy": source.policy.value,
                "policy_reason": source.policy_reason,
                "status": source.status.value,
                "moved_from_uri": source.moved_from_uri,
                "current_version_id": source.current_version_id,
                "secret_suspected": source.secret_suspected,
                "origin": source.origin.value,
                "trust": source.trust.value,
                "first_seen_at": source.first_seen_at,
                "last_seen_at": source.last_seen_at,
            },
        ).first()
        return Source(**_m(row))

    def get_by_uri(self, uri: str) -> Source | None:
        row = self._s.execute(text("SELECT * FROM sources WHERE uri = :uri"), {"uri": uri}).first()
        return Source(**_m(row)) if row is not None else None

    def record_version(self, version: SourceVersion) -> tuple[SourceVersion, bool]:
        """Idempotency rule 2: ``ON CONFLICT (source_id, content_hash) DO NOTHING``.

        Returns ``(version, created)``; ``created=False`` means identical bytes were already known
        and the *existing* row is returned untouched.
        """
        row = self._s.execute(
            text(
                """
                INSERT INTO source_versions (id, source_id, content_hash, size_bytes, mtime,
                                              git_commit, git_branch, observed_at,
                                              ingestion_run_id, is_current, change_type, created_at)
                VALUES (:id, :source_id, :content_hash, :size_bytes, :mtime, :git_commit,
                        :git_branch, :observed_at, :ingestion_run_id, :is_current, :change_type,
                        :created_at)
                ON CONFLICT (source_id, content_hash) DO NOTHING
                RETURNING *
                """
            ),
            version.model_dump(),
        ).first()
        if row is not None:
            return SourceVersion(**_m(row)), True
        existing = self._s.execute(
            text(
                "SELECT * FROM source_versions WHERE source_id = :sid AND content_hash = :h"
            ),
            {"sid": version.source_id, "h": version.content_hash},
        ).first()
        return SourceVersion(**_m(existing)), False

    def set_current_version(self, source_id: UUID, version_id: UUID) -> None:
        self._s.execute(
            text("UPDATE source_versions SET is_current = false WHERE source_id = :sid"),
            {"sid": source_id},
        )
        self._s.execute(
            text("UPDATE source_versions SET is_current = true WHERE id = :vid"),
            {"vid": version_id},
        )
        self._s.execute(
            text("UPDATE sources SET current_version_id = :vid, last_seen_at = now() WHERE id = :sid"),
            {"vid": version_id, "sid": source_id},
        )

    def mark_moved(self, source_id: UUID, new_uri: str, moved_from_uri: str) -> Source:
        """ADR-0005 rule 4 (moved): same id, new uri, no re-extraction."""
        row = self._s.execute(
            text(
                """
                UPDATE sources
                   SET uri = :uri, moved_from_uri = :moved_from, status = 'active',
                       last_seen_at = now()
                 WHERE id = :id
                RETURNING *
                """
            ),
            {"uri": new_uri, "moved_from": moved_from_uri, "id": source_id},
        ).first()
        return Source(**_m(row))

    def mark_deleted(self, source_id: UUID) -> Source:
        """ADR-0005 rule 4: flag, never erase; propagate to derived facts/artifacts."""
        row = self._s.execute(
            text(
                "UPDATE sources SET status = 'deleted' WHERE id = :id RETURNING *"
            ),
            {"id": source_id},
        ).first()
        self._s.execute(
            text("UPDATE facts SET source_status = 'deleted' WHERE source_id = :id"),
            {"id": source_id},
        )
        self._s.execute(
            text("UPDATE knowledge_artifacts SET source_status = 'deleted' WHERE source_id = :id"),
            {"id": source_id},
        )
        return Source(**_m(row))

    def restore(self, source_id: UUID) -> Source:
        row = self._s.execute(
            text("UPDATE sources SET status = 'active' WHERE id = :id RETURNING *"),
            {"id": source_id},
        ).first()
        self._s.execute(
            text("UPDATE facts SET source_status = 'active' WHERE source_id = :id"),
            {"id": source_id},
        )
        self._s.execute(
            text("UPDATE knowledge_artifacts SET source_status = 'active' WHERE source_id = :id"),
            {"id": source_id},
        )
        return Source(**_m(row))

    def upsert_text(self, source_text: SourceText) -> SourceText:
        row = self._s.execute(
            text(
                """
                INSERT INTO source_text (version_id, text, extractor, extractor_version,
                                          char_count, truncated, frontmatter, links, created_at)
                VALUES (:version_id, :text, :extractor, :extractor_version, :char_count,
                        :truncated, :frontmatter, :links, :created_at)
                ON CONFLICT (version_id) DO UPDATE SET
                    text = EXCLUDED.text, extractor = EXCLUDED.extractor,
                    extractor_version = EXCLUDED.extractor_version, char_count = EXCLUDED.char_count,
                    truncated = EXCLUDED.truncated, frontmatter = EXCLUDED.frontmatter,
                    links = EXCLUDED.links
                RETURNING *
                """
            ),
            _wrap_jsonb(source_text.model_dump(), "frontmatter"),
        ).first()
        return SourceText(**_m(row))

    def record_event(self, event: SourceEvent) -> SourceEvent:
        """``source_events`` is append-only: plain INSERT, no conflict target."""
        row = self._s.execute(
            text(
                """
                INSERT INTO source_events (id, source_id, version_id, event_type, at, run_id, details)
                VALUES (:id, :source_id, :version_id, :event_type, :at, :run_id, :details)
                RETURNING *
                """
            ),
            _wrap_jsonb(
                {**event.model_dump(exclude={"event_type"}), "event_type": event.event_type.value},
                "details",
            ),
        ).first()
        return SourceEvent(**_m(row))


class ChunkRepo:
    """``chunks``. Deterministic ids make replayed writes a no-op (plan §L)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def bulk_insert(self, chunks: Sequence[Chunk]) -> int:
        if not chunks:
            return 0
        count = 0
        for chunk in chunks:
            self._s.execute(
                text(
                    """
                    INSERT INTO chunks (id, version_id, source_id, project_id, ordinal, text,
                                         text_hash, heading_path, char_start, char_end,
                                         token_count, created_at)
                    VALUES (:id, :version_id, :source_id, :project_id, :ordinal, :text,
                            :text_hash, :heading_path, :char_start, :char_end, :token_count,
                            :created_at)
                    ON CONFLICT (version_id, ordinal) DO UPDATE SET
                        text = EXCLUDED.text, text_hash = EXCLUDED.text_hash,
                        heading_path = EXCLUDED.heading_path, char_start = EXCLUDED.char_start,
                        char_end = EXCLUDED.char_end, token_count = EXCLUDED.token_count
                    """
                ),
                chunk.model_dump(),
            )
            count += 1
        return count

    _COLUMNS = (
        "id, version_id, source_id, project_id, ordinal, text, text_hash, heading_path, "
        "char_start, char_end, token_count, created_at"
    )

    def get_by_version(self, version_id: UUID) -> list[Chunk]:
        rows = self._s.execute(
            text(f"SELECT {self._COLUMNS} FROM chunks WHERE version_id = :vid ORDER BY ordinal"),
            {"vid": version_id},
        ).all()
        return [Chunk(**_m(r)) for r in rows]


class EmbeddingRepo:
    """``embeddings``. Reuse key: ``(text_hash, model_id)`` (plan section L)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def get_by_hash(self, text_hash: str, model_id: str) -> Embedding | None:
        row = self._s.execute(
            text("SELECT * FROM embeddings WHERE text_hash = :h AND model_id = :m"),
            {"h": text_hash, "m": model_id},
        ).first()
        if row is None:
            return None
        m = _m(row)
        m["vector"] = _vector_to_list(m["vector"])
        return Embedding(**m)

    def bulk_insert(self, embeddings: Sequence[Embedding]) -> int:
        """Idempotency rule 3: ``ON CONFLICT (text_hash, model_id) DO NOTHING``."""
        count = 0
        for emb in embeddings:
            result = self._s.execute(
                text(
                    """
                    INSERT INTO embeddings (id, object_type, object_id, text_hash, model_id,
                                             dimension, vector, created_at)
                    VALUES (:id, :object_type, :object_id, :text_hash, :model_id, :dimension,
                            :vector, :created_at)
                    ON CONFLICT (text_hash, model_id) DO NOTHING
                    """
                ),
                {
                    "id": emb.id,
                    "object_type": emb.object_type.value,
                    "object_id": emb.object_id,
                    "text_hash": emb.text_hash,
                    "model_id": emb.model_id,
                    "dimension": emb.dimension,
                    "vector": emb.vector,
                    "created_at": emb.created_at,
                },
            )
            count += result.rowcount
        return count


# ------------------------------------------------------------------------------------------------
# Episodes and knowledge
# ------------------------------------------------------------------------------------------------


class EpisodeRepo:
    """``episodes`` - the Tier 2 queue (ADR-0006)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def create(self, episode: Episode) -> Episode:
        row = self._s.execute(
            text(
                """
                INSERT INTO episodes (id, type, source_id, version_id, project_id, title,
                                       section_path, body, occurred_at, observed_at, status,
                                       attempts, engine, graph_episode_uuid, origin,
                                       origin_client, tier, priority, error, ingestion_run_id,
                                       created_at, updated_at)
                VALUES (:id, :type, :source_id, :version_id, :project_id, :title, :section_path,
                        :body, :occurred_at, :observed_at, :status, :attempts, :engine,
                        :graph_episode_uuid, :origin, :origin_client, :tier, :priority, :error,
                        :ingestion_run_id, :created_at, :updated_at)
                ON CONFLICT (version_id, section_path) WHERE version_id IS NOT NULL DO UPDATE SET
                    observed_at = EXCLUDED.observed_at
                RETURNING *
                """
            ),
            {
                **episode.model_dump(exclude={"type", "status", "engine", "origin", "tier"}),
                "type": episode.type.value,
                "status": episode.status.value,
                "engine": episode.engine.value if episode.engine else None,
                "origin": episode.origin.value,
                "tier": int(episode.tier),
            },
        ).first()
        return Episode(**_m(row))

    def claim_next(self) -> Episode | None:
        """Pop the highest-priority queued episode (``SKIP LOCKED``, safe under one worker only -
        ADR-0006 fixes concurrency at 1 LLM call, so this is a simple, not a hot, path)."""
        row = self._s.execute(
            text(
                """
                UPDATE episodes SET status = 'running', updated_at = now()
                 WHERE id = (
                    SELECT id FROM episodes
                     WHERE status IN ('pending', 'queued')
                     ORDER BY priority ASC, created_at ASC
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                 )
                RETURNING *
                """
            )
        ).first()
        return Episode(**_m(row)) if row is not None else None

    def mark_extracted(self, episode_id: UUID, engine: str) -> Episode:
        row = self._s.execute(
            text(
                """
                UPDATE episodes SET status = 'extracted', engine = :engine, updated_at = now()
                 WHERE id = :id
                RETURNING *
                """
            ),
            {"id": episode_id, "engine": engine},
        ).first()
        return Episode(**_m(row))

    def mark_failed(self, episode_id: UUID, error: str) -> Episode:
        row = self._s.execute(
            text(
                """
                UPDATE episodes
                   SET status = 'failed', attempts = attempts + 1, error = :error, updated_at = now()
                 WHERE id = :id
                RETURNING *
                """
            ),
            {"id": episode_id, "error": error},
        ).first()
        return Episode(**_m(row))

    def get(self, episode_id: UUID) -> Episode | None:
        row = self._s.execute(
            text("SELECT * FROM episodes WHERE id = :id"), {"id": episode_id}
        ).first()
        return Episode(**_m(row)) if row is not None else None


class EntityRepo:
    """``entities`` / ``entity_mentions`` - deterministic-first resolution (ontology.md §7)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def find_by_normalized_name(
        self, type_: str, normalized_name: str, project_id: str | None
    ) -> Entity | None:
        row = self._s.execute(
            text(
                """
                SELECT * FROM entities
                 WHERE type = :type AND normalized_name = :name
                   AND coalesce(project_id, '') = coalesce(:project_id, '')
                """
            ),
            {"type": type_, "name": normalized_name, "project_id": project_id},
        ).first()
        return Entity(**_m(row)) if row is not None else None

    def trigram_candidates(self, normalized_name: str, *, limit: int = 5) -> list[Entity]:
        rows = self._s.execute(
            text(
                """
                SELECT * FROM entities
                 WHERE normalized_name % :name
                 ORDER BY similarity(normalized_name, :name) DESC
                 LIMIT :limit
                """
            ),
            {"name": normalized_name, "limit": limit},
        ).all()
        return [Entity(**_m(r)) for r in rows]

    def upsert(self, entity: Entity) -> Entity:
        row = self._s.execute(
            text(
                """
                INSERT INTO entities (id, type, canonical_name, normalized_name, aliases,
                                       project_id, summary, status, merged_into_id, confidence,
                                       engine, first_seen_at, last_seen_at)
                VALUES (:id, :type, :canonical_name, :normalized_name, :aliases, :project_id,
                        :summary, :status, :merged_into_id, :confidence, :engine, :first_seen_at,
                        :last_seen_at)
                ON CONFLICT (type, normalized_name, coalesce(project_id, '')) DO UPDATE SET
                    canonical_name = EXCLUDED.canonical_name,
                    aliases = coalesce((SELECT array_agg(DISTINCT a) FROM unnest(
                        entities.aliases || EXCLUDED.aliases) AS a), '{}'),
                    summary = coalesce(EXCLUDED.summary, entities.summary),
                    last_seen_at = EXCLUDED.last_seen_at
                RETURNING *
                """
            ),
            {
                "id": entity.id,
                "type": entity.type.value,
                "canonical_name": entity.canonical_name,
                "normalized_name": entity.normalized_name,
                "aliases": entity.aliases,
                "project_id": entity.project_id,
                "summary": entity.summary,
                "status": entity.status,
                "merged_into_id": entity.merged_into_id,
                "confidence": entity.confidence,
                "engine": entity.engine.value,
                "first_seen_at": entity.first_seen_at,
                "last_seen_at": entity.last_seen_at,
            },
        ).first()
        return Entity(**_m(row))

    def add_mention(self, mention: EntityMention) -> EntityMention:
        p = _prov_dict(mention.provenance)
        row = self._s.execute(
            text(
                """
                INSERT INTO entity_mentions (id, entity_id, episode_id, chunk_id, surface_form,
                                              char_start, char_end, confidence, source_id,
                                              source_uri, source_hash, source_version, project_id,
                                              device_id, observed_at, valid_from, valid_to,
                                              extraction_model_id, embedding_model_id,
                                              ingestion_run_id, heading_path)
                VALUES (:id, :entity_id, :episode_id, :chunk_id, :surface_form, :char_start,
                        :char_end, :confidence, :source_id, :source_uri, :source_hash,
                        :source_version, :project_id, :device_id, :observed_at, :valid_from,
                        :valid_to, :extraction_model_id, :embedding_model_id, :ingestion_run_id,
                        :heading_path)
                RETURNING *
                """
            ),
            {
                "id": mention.id,
                "entity_id": mention.entity_id,
                "episode_id": mention.episode_id,
                "chunk_id": mention.chunk_id,
                "surface_form": mention.surface_form,
                "char_start": mention.char_start,
                "char_end": mention.char_end,
                "confidence": mention.confidence,
                "source_id": p["source_id"],
                "source_uri": p["source_uri"],
                "source_hash": p["source_hash"],
                "source_version": p["source_version"],
                "project_id": p["project_id"],
                "device_id": p["device_id"],
                "observed_at": p["observed_at"],
                "valid_from": p["valid_from"],
                "valid_to": p["valid_to"],
                "extraction_model_id": p["extraction_model_id"],
                "embedding_model_id": p["embedding_model_id"],
                "ingestion_run_id": p["ingestion_run_id"],
                "heading_path": mention.provenance.heading_path,
            },
        ).first()
        return _mention_from_row(_m(row))


def _mention_from_row(m: dict[str, Any]) -> EntityMention:
    return EntityMention(
        id=m["id"],
        entity_id=m["entity_id"],
        episode_id=m["episode_id"],
        chunk_id=m.get("chunk_id"),
        surface_form=m["surface_form"],
        char_start=m.get("char_start"),
        char_end=m.get("char_end"),
        confidence=m["confidence"],
        provenance=_provenance_from_row(m),
    )


class FactRepo:
    """``facts`` - the ADR-0005 temporal store. The supersession algorithm itself lives in A08's
    ``knowledge/temporal/`` (docs/architecture/temporal.md); this repo only offers the primitives
    it composes: find the open fact, insert a new one, close one, link a supersession.
    """

    def __init__(self, session: Session) -> None:
        self._s = session

    def find_open_fact(self, subject_entity_id: UUID, predicate: str) -> Fact | None:
        row = self._s.execute(
            text(
                """
                SELECT * FROM facts
                 WHERE subject_entity_id = :subject AND predicate = :predicate
                   AND valid_to IS NULL
                """
            ),
            {"subject": subject_entity_id, "predicate": predicate},
        ).first()
        return _fact_from_row(_m(row)) if row is not None else None

    def insert(self, fact: Fact) -> Fact:
        p = _prov_dict(fact.provenance)
        row = self._s.execute(
            text(
                """
                INSERT INTO facts (id, subject_entity_id, predicate, object_entity_id,
                                    object_value, statement, valid_from, valid_to, observed_at,
                                    status, source_status, invalidated_at,
                                    invalidated_by_episode_id, supersedes_fact_id, confidence,
                                    engine, project_id, source_id, source_uri, source_hash,
                                    source_version, device_id, extraction_model_id,
                                    embedding_model_id, ingestion_run_id, episode_id)
                VALUES (:id, :subject_entity_id, :predicate, :object_entity_id, :object_value,
                        :statement, :valid_from, :valid_to, :observed_at, :status, :source_status,
                        :invalidated_at, :invalidated_by_episode_id, :supersedes_fact_id,
                        :confidence, :engine, :project_id, :source_id, :source_uri, :source_hash,
                        :source_version, :device_id, :extraction_model_id, :embedding_model_id,
                        :ingestion_run_id, :episode_id)
                RETURNING *
                """
            ),
            {
                "id": fact.id,
                "subject_entity_id": fact.subject_entity_id,
                "predicate": fact.predicate.value,
                "object_entity_id": fact.object_entity_id,
                "object_value": fact.object_value,
                "statement": fact.statement,
                "valid_from": fact.valid_from,
                "valid_to": fact.valid_to,
                "observed_at": fact.observed_at,
                "status": fact.status.value,
                "source_status": fact.source_status.value,
                "invalidated_at": fact.invalidated_at,
                "invalidated_by_episode_id": fact.invalidated_by_episode_id,
                "supersedes_fact_id": fact.supersedes_fact_id,
                "confidence": fact.confidence,
                "engine": fact.engine.value,
                "project_id": fact.project_id,
                "source_id": p["source_id"],
                "source_uri": p["source_uri"],
                "source_hash": p["source_hash"],
                "source_version": p["source_version"],
                "device_id": p["device_id"],
                "extraction_model_id": p["extraction_model_id"],
                "embedding_model_id": p["embedding_model_id"],
                "ingestion_run_id": p["ingestion_run_id"],
                "episode_id": p["episode_id"],
            },
        ).first()
        return _fact_from_row(_m(row))

    def close_fact(self, fact_id: UUID, at: datetime, by_episode: UUID | None) -> Fact:
        """``temporal.md`` ``close()``: idempotent - re-closing at the same instant is a no-op."""
        row = self._s.execute(
            text(
                """
                UPDATE facts
                   SET valid_to = :at, status = 'historical', invalidated_at = now(),
                       invalidated_by_episode_id = coalesce(:by_episode, invalidated_by_episode_id)
                 WHERE id = :id AND (valid_to IS NULL OR valid_to <> :at)
                RETURNING *
                """
            ),
            {"id": fact_id, "at": at, "by_episode": by_episode},
        ).first()
        if row is None:
            existing = self._s.execute(
                text("SELECT * FROM facts WHERE id = :id"), {"id": fact_id}
            ).first()
            return _fact_from_row(_m(existing))
        return _fact_from_row(_m(row))

    def touch(
        self, fact_id: UUID, *, observed_at: datetime, status: str, confidence: float
    ) -> Fact:
        """``apply_fact`` 'reconfirmed' branch: refresh instead of inserting a duplicate row."""
        row = self._s.execute(
            text(
                """
                UPDATE facts
                   SET observed_at = GREATEST(observed_at, :observed_at), status = :status,
                       confidence = GREATEST(confidence, :confidence)
                 WHERE id = :id
                RETURNING *
                """
            ),
            {"id": fact_id, "observed_at": observed_at, "status": status, "confidence": confidence},
        ).first()
        return _fact_from_row(_m(row))

    def insert_superseding(self, new_fact: Fact, old_fact_id: UUID) -> Fact:
        """Insert ``new_fact`` with ``supersedes_fact_id`` set, in the same call site's transaction
        as the caller's :meth:`close_fact` on ``old_fact_id`` (ADR-0005 rule 1/2)."""
        if new_fact.supersedes_fact_id != old_fact_id:
            new_fact = new_fact.model_copy(update={"supersedes_fact_id": old_fact_id})
        return self.insert(new_fact)


def _fact_from_row(m: dict[str, Any]) -> Fact:
    return Fact(
        id=m["id"],
        subject_entity_id=m["subject_entity_id"],
        predicate=m["predicate"],
        object_entity_id=m.get("object_entity_id"),
        object_value=m.get("object_value"),
        statement=m["statement"],
        valid_from=m["valid_from"],
        valid_to=m.get("valid_to"),
        observed_at=m["observed_at"],
        status=m["status"],
        source_status=m["source_status"],
        invalidated_at=m.get("invalidated_at"),
        invalidated_by_episode_id=m.get("invalidated_by_episode_id"),
        supersedes_fact_id=m.get("supersedes_fact_id"),
        confidence=m["confidence"],
        engine=m["engine"],
        project_id=m.get("project_id"),
        provenance=_provenance_from_row(m),
    )


class ArtifactRepo:
    """``knowledge_artifacts`` / ``artifact_entities``."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def insert(self, artifact: KnowledgeArtifact) -> KnowledgeArtifact:
        p = _prov_dict(artifact.provenance)
        row = self._s.execute(
            text(
                """
                INSERT INTO knowledge_artifacts (id, type, title, body, structured, project_id,
                                                  current_status, valid_from, valid_to,
                                                  supersedes_id, superseded_by_id, confidence,
                                                  evidence_quote, engine, source_status, source_id,
                                                  source_uri, source_hash, source_version,
                                                  device_id, observed_at, extraction_model_id,
                                                  embedding_model_id, ingestion_run_id, episode_id)
                VALUES (:id, :type, :title, :body, :structured, :project_id, :current_status,
                        :valid_from, :valid_to, :supersedes_id, :superseded_by_id, :confidence,
                        :evidence_quote, :engine, :source_status, :source_id, :source_uri,
                        :source_hash, :source_version, :device_id, :observed_at,
                        :extraction_model_id, :embedding_model_id, :ingestion_run_id, :episode_id)
                RETURNING *
                """
            ),
            _wrap_jsonb({
                "id": artifact.id,
                "type": artifact.type.value,
                "title": artifact.title,
                "body": artifact.body,
                "structured": artifact.structured,
                "project_id": artifact.project_id,
                "current_status": artifact.current_status.value,
                "valid_from": artifact.valid_from,
                "valid_to": artifact.valid_to,
                "supersedes_id": artifact.supersedes_id,
                "superseded_by_id": artifact.superseded_by_id,
                "confidence": artifact.confidence,
                "evidence_quote": artifact.evidence_quote,
                "engine": artifact.engine.value,
                "source_status": artifact.source_status.value,
                "source_id": p["source_id"],
                "source_uri": p["source_uri"],
                "source_hash": p["source_hash"],
                "source_version": p["source_version"],
                "device_id": p["device_id"],
                "observed_at": p["observed_at"],
                "extraction_model_id": p["extraction_model_id"],
                "embedding_model_id": p["embedding_model_id"],
                "ingestion_run_id": p["ingestion_run_id"],
                "episode_id": p["episode_id"],
            }, "structured"),
        ).first()
        return _artifact_from_row(_m(row))

    def get(self, artifact_id: UUID) -> KnowledgeArtifact | None:
        row = self._s.execute(
            text("SELECT * FROM knowledge_artifacts WHERE id = :id"), {"id": artifact_id}
        ).first()
        return _artifact_from_row(_m(row)) if row is not None else None

    def supersede(self, old_id: UUID, new_id: UUID, at: datetime) -> None:
        """ADR-0005 rule 6: a state transition, never a deletion."""
        self._s.execute(
            text(
                """
                UPDATE knowledge_artifacts
                   SET current_status = 'superseded', valid_to = :at, superseded_by_id = :new_id
                 WHERE id = :old_id
                """
            ),
            {"old_id": old_id, "new_id": new_id, "at": at},
        )

    def link_entity(self, artifact_entity: ArtifactEntity) -> None:
        self._s.execute(
            text(
                """
                INSERT INTO artifact_entities (artifact_id, entity_id, role)
                VALUES (:artifact_id, :entity_id, :role)
                ON CONFLICT (artifact_id, entity_id, role) DO NOTHING
                """
            ),
            artifact_entity.model_dump(),
        )


def _artifact_from_row(m: dict[str, Any]) -> KnowledgeArtifact:
    return KnowledgeArtifact(
        id=m["id"],
        type=m["type"],
        title=m["title"],
        body=m["body"],
        structured=m.get("structured") or {},
        project_id=m.get("project_id"),
        current_status=m["current_status"],
        valid_from=m["valid_from"],
        valid_to=m.get("valid_to"),
        supersedes_id=m.get("supersedes_id"),
        superseded_by_id=m.get("superseded_by_id"),
        confidence=m["confidence"],
        evidence_quote=m.get("evidence_quote"),
        engine=m["engine"],
        source_status=m["source_status"],
        provenance=_provenance_from_row(m),
    )


# ------------------------------------------------------------------------------------------------
# Operations and audit
# ------------------------------------------------------------------------------------------------


class RunRepo:
    """``ingestion_runs``."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def create(self, run: IngestionRun) -> IngestionRun:
        row = self._s.execute(
            text(
                """
                INSERT INTO ingestion_runs (id, root_id, tier, trigger, requested_by, status,
                                             started_at, finished_at, counters, error)
                VALUES (:id, :root_id, :tier, :trigger, :requested_by, :status, :started_at,
                        :finished_at, :counters, :error)
                RETURNING *
                """
            ),
            _wrap_jsonb(
                {
                    "id": run.id,
                    "root_id": run.root_id,
                    "tier": int(run.tier),
                    "trigger": run.trigger.value,
                    "requested_by": run.requested_by,
                    "status": run.status.value,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "counters": run.counters,
                    "error": run.error,
                },
                "counters",
            ),
        ).first()
        return IngestionRun(**_m(row))

    def finish(self, run_id: UUID, *, status: RunStatus, counters: Mapping[str, int], error: str | None = None) -> IngestionRun:
        row = self._s.execute(
            text(
                """
                UPDATE ingestion_runs
                   SET status = :status, counters = :counters, error = :error, finished_at = now()
                 WHERE id = :id
                RETURNING *
                """
            ),
            _wrap_jsonb(
                {"id": run_id, "status": status.value, "counters": dict(counters), "error": error},
                "counters",
            ),
        ).first()
        return IngestionRun(**_m(row))


class JobRepo:
    """``ingestion_jobs`` - the resumable per-``(version_id, stage)`` state machine (plan §L)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def upsert(self, job: IngestionJob) -> IngestionJob:
        """Idempotency rule 1: ``ON CONFLICT (version_id, stage) DO UPDATE``."""
        row = self._s.execute(
            text(
                """
                INSERT INTO ingestion_jobs (id, run_id, version_id, source_id, stage, state,
                                             attempts, started_at, finished_at, duration_ms, error)
                VALUES (:id, :run_id, :version_id, :source_id, :stage, :state, :attempts,
                        :started_at, :finished_at, :duration_ms, :error)
                ON CONFLICT (version_id, stage) DO UPDATE SET
                    state = EXCLUDED.state, attempts = ingestion_jobs.attempts + 1,
                    started_at = coalesce(EXCLUDED.started_at, ingestion_jobs.started_at),
                    finished_at = EXCLUDED.finished_at, duration_ms = EXCLUDED.duration_ms,
                    error = EXCLUDED.error
                RETURNING *
                """
            ),
            {
                "id": job.id,
                "run_id": job.run_id,
                "version_id": job.version_id,
                "source_id": job.source_id,
                "stage": job.stage.value,
                "state": job.state.value,
                "attempts": job.attempts,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "duration_ms": job.duration_ms,
                "error": job.error,
            },
        ).first()
        return IngestionJob(**_m(row))

    def requeue_stuck(self) -> int:
        """Restart policy (plan §L): rows left ``running`` by a killed process are re-queued."""
        result = self._s.execute(
            text("UPDATE ingestion_jobs SET state = 'pending' WHERE state = 'running'")
        )
        return result.rowcount


class AuditRepo:
    """``mcp_audit_log`` (ADR-0008) and ``retrieval_logs`` (plan section P) - both append-only."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def log_mcp_call(self, entry: McpAuditLog) -> McpAuditLog:
        row = self._s.execute(
            text(
                """
                INSERT INTO mcp_audit_log (id, at, tool, kind, client_id, arguments, confirmed,
                                            allowed, denied_reason, result_ref, latency_ms)
                VALUES (:id, :at, :tool, :kind, :client_id, :arguments, :confirmed, :allowed,
                        :denied_reason, :result_ref, :latency_ms)
                RETURNING *
                """
            ),
            _wrap_jsonb(entry.model_dump(), "arguments"),
        ).first()
        return McpAuditLog(**_m(row))

    def log_retrieval(self, entry: RetrievalLog) -> RetrievalLog:
        params = _wrap_jsonb(
            {
                "id": entry.id,
                "at": entry.at,
                "query_text": entry.query_text,
                "query_hash": entry.query_hash,
                "project_ids": entry.project_ids,
                "object_types": [t.value for t in entry.object_types],
                "as_of": entry.as_of,
                "since": entry.since,
                "limit": entry.limit,
                "expand": entry.expand,
                "params": entry.params,
                "candidate_counts": entry.candidate_counts,
                "result_ids": entry.result_ids,
                "latency_ms": entry.latency_ms,
                "client": entry.client,
                "warnings": entry.warnings,
            },
            "params",
            "candidate_counts",
        )
        row = self._s.execute(
            text(
                """
                INSERT INTO retrieval_logs (id, at, query_text, query_hash, project_ids,
                                             object_types, as_of, since, "limit", expand, params,
                                             candidate_counts, result_ids, latency_ms, client,
                                             warnings)
                VALUES (:id, :at, :query_text, :query_hash, :project_ids, :object_types, :as_of,
                        :since, :limit, :expand, :params, :candidate_counts, :result_ids,
                        :latency_ms, :client, :warnings)
                RETURNING *
                """
            ),
            params,
        ).first()
        return RetrievalLog(**_m(row))


class MetricsRepo:
    """``metrics_snapshots`` / ``extraction_reviews`` / ``run_requests`` / ``service_stats``
    (ADR-0010/ADR-0011)."""

    def __init__(self, session: Session) -> None:
        self._s = session

    def insert_snapshot(self, snapshot: MetricsSnapshot) -> MetricsSnapshot:
        row = self._s.execute(
            text(
                """
                INSERT INTO metrics_snapshots (id, at, scope, run_id, model_id,
                    schema_validity_rate, failed_episode_share, median_seconds_per_episode,
                    duplicate_entity_rate, unconfirmed_fact_share, coverage_by_project,
                    gold_hit_at_5, expected_entity_presence, provenance_completeness, extra)
                VALUES (:id, :at, :scope, :run_id, :model_id, :schema_validity_rate,
                    :failed_episode_share, :median_seconds_per_episode, :duplicate_entity_rate,
                    :unconfirmed_fact_share, :coverage_by_project, :gold_hit_at_5,
                    :expected_entity_presence, :provenance_completeness, :extra)
                RETURNING *
                """
            ),
            _wrap_jsonb(snapshot.model_dump(), "coverage_by_project", "extra"),
        ).first()
        return MetricsSnapshot(**_m(row))

    def latest(self, scope: str) -> MetricsSnapshot | None:
        row = self._s.execute(
            text(
                "SELECT * FROM metrics_snapshots WHERE scope = :scope ORDER BY at DESC LIMIT 1"
            ),
            {"scope": scope},
        ).first()
        return MetricsSnapshot(**_m(row)) if row is not None else None

    def add_review(self, review: ExtractionReview) -> ExtractionReview:
        row = self._s.execute(
            text(
                """
                INSERT INTO extraction_reviews (id, at, object_type, object_id, verdict,
                                                 reviewer, note, episode_id, model_id, sample_batch)
                VALUES (:id, :at, :object_type, :object_id, :verdict, :reviewer, :note,
                        :episode_id, :model_id, :sample_batch)
                RETURNING *
                """
            ),
            {**review.model_dump(exclude={"object_type", "verdict"}),
             "object_type": review.object_type.value, "verdict": review.verdict.value},
        ).first()
        return ExtractionReview(**_m(row))

    def enqueue_run_request(self, request: RunRequest) -> RunRequest:
        row = self._s.execute(
            text(
                """
                INSERT INTO run_requests (id, action, root_id, tier, options, requested_by,
                                           requested_at, status, progress_pct, message,
                                           started_at, finished_at, run_id, error)
                VALUES (:id, :action, :root_id, :tier, :options, :requested_by, :requested_at,
                        :status, :progress_pct, :message, :started_at, :finished_at, :run_id,
                        :error)
                RETURNING *
                """
            ),
            _wrap_jsonb(
                {
                    **request.model_dump(exclude={"action", "tier", "status"}),
                    "action": request.action.value,
                    "tier": int(request.tier),
                    "status": request.status.value,
                },
                "options",
            ),
        ).first()
        return RunRequest(**_m(row))

    def claim_next_run_request(self) -> RunRequest | None:
        row = self._s.execute(
            text(
                """
                UPDATE run_requests SET status = 'running', started_at = now()
                 WHERE id = (
                    SELECT id FROM run_requests WHERE status = 'queued'
                    ORDER BY requested_at ASC FOR UPDATE SKIP LOCKED LIMIT 1
                 )
                RETURNING *
                """
            )
        ).first()
        return RunRequest(**_m(row)) if row is not None else None

    def record_service_stat(self, stat: ServiceStat) -> ServiceStat:
        row = self._s.execute(
            text(
                """
                INSERT INTO service_stats (id, at, service, process_rss_bytes, model_loaded,
                                            model_name, details)
                VALUES (:id, :at, :service, :process_rss_bytes, :model_loaded, :model_name,
                        :details)
                RETURNING *
                """
            ),
            _wrap_jsonb(stat.model_dump(), "details"),
        ).first()
        return ServiceStat(**_m(row))
