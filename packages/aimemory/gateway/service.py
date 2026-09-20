"""The Memory Gateway service functions (plan section Q). Owner: A09.

``apps/memory-api`` is a thin REST skin over this class and ``apps/mcp-server`` (A10) calls the same
functions through that REST surface - so every access rule lives *here*, once:

* **Project scoping, temporal filtering and provenance** are applied inside the service, not by the
  caller. A route cannot forget them.
* **Sources resolve to URIs, never bytes** (plan section Q).
* **Writes are refused unless ``GATEWAY_WRITE_ENABLED``** and are append-only (ADR-0008). The MCP
  layer adds ``confirm``, a rate limit and its own audit row on top; neither layer can be bypassed by
  the other.
* **Degradation is visible.** Neo4j down -> vector+keyword with a warning; embedding service down ->
  keyword-only with a warning. Both travel in ``SearchResult.warnings`` and both are proved by tests
  that simulate the outage.
* **Business and research results are labelled, never merged silently.** Every project-scoped view
  carries its ``track`` and a mixed answer is flagged in ``warnings``.

The session boundary is the caller's: :class:`Gateway` is constructed with a ``session_scope``
callable (``Database.session`` in production, a rollback-only fixture in tests) and opens exactly one
transaction per public call, so the search, its provenance resolution and its ``retrieval_logs`` row
are one consistent read.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from ..common.config import Settings, get_settings
from ..common.errors import NotFoundError
from ..common.logging import get_logger
from ..common.time import ensure_utc, utc_now
from ..domain.enums import ObjectType, SourceStatus, Trust
from ..domain.ports import EmbeddingProvider, GraphStore
from ..domain.provenance import Provenance
from ..domain.retrieval import (
    AssembledContext,
    ExplainChain,
    RelatedEntity,
    RetrievalConfig,
    ScoredHit,
    SearchQuery,
    SearchResult,
)
from ..provenance.explain import explain as explain_chain
from ..retrieval.config import load_retrieval_config
from ..retrieval.context import (
    ContextInputs,
    DecisionRow,
    ProjectSummary,
    assemble_context,
    track_mix_warning,
)
from ..retrieval.expansion import GraphExpansion, expand
from ..retrieval.logs import write_retrieval_log
from ..retrieval.pipeline import HybridRetriever, RetrievalOutcome
from ..retrieval.provenance import attach_provenance
from ..retrieval.staleness import staleness_warnings
from ..retrieval.temporal import TEMPORAL_DROP_WARNING, facts_for_entities, filter_hits
from ..retrieval.types import HitKey, RankedCandidate
from . import queries
from .models import (
    ArtifactView,
    ComponentHealth,
    CurrentState,
    EntityView,
    HealthReport,
    ProjectView,
    RelatedResult,
    SourceView,
    TimelineEvent,
    WriteReceipt,
)
from .writes import add_episode as _add_episode
from .writes import record_mcp_audit as _record_mcp_audit
from .writes import record_decision as _record_decision

__all__ = ["Gateway", "SessionScope"]

logger = get_logger(__name__)

#: ``Database.session`` satisfies this: a callable returning a transactional context manager.
SessionScope = Callable[[], AbstractContextManager[Session]]


class Gateway:
    """Every plan section Q function, over one session per call."""

    def __init__(
        self,
        session_scope: SessionScope,
        *,
        graph: GraphStore | None = None,
        embedder: EmbeddingProvider | None = None,
        config: RetrievalConfig | None = None,
        settings: Settings | None = None,
        retriever: HybridRetriever | None = None,
    ) -> None:
        self._session_scope = session_scope
        self._graph = graph
        self._embedder = embedder
        self._settings = settings or get_settings()
        self._config = config or load_retrieval_config()
        self._retriever = retriever or HybridRetriever(embedder, config=self._config)

    # ------------------------------------------------------------------------------ properties

    @property
    def config(self) -> RetrievalConfig:
        return self._config

    @property
    def writes_enabled(self) -> bool:
        """ADR-0008 half one. The MCP layer owns the other half plus ``confirm``."""
        return self._settings.gateway.write_enabled

    @property
    def device_id(self) -> str:
        return self._settings.device_id

    def _session(self) -> AbstractContextManager[Session]:
        return self._session_scope()

    # --------------------------------------------------------------------------------- search

    def search(self, query: SearchQuery, *, now: datetime | None = None) -> SearchResult:
        """Stages 1-9 of ``retrieval.md`` for one query. Never raises on a degraded dependency."""
        started = time.perf_counter()
        moment = ensure_utc(now) if now is not None else utc_now()
        with self._session() as session:
            outcome = self._retriever.retrieve(session, query, now=moment)
            expansion = self._expand(session, query, outcome, moment)
            outcome = self._retriever.rerank(
                outcome, query, entity_linked_keys=expansion.entity_linked_keys, now=moment
            )

            kept, dropped = filter_hits(
                outcome.hits,
                as_of=query.as_of or moment,
                include_unconfirmed=query.include_unconfirmed,
            )
            flags = _hit_flags(kept)
            hits, provenance_warnings = attach_provenance(
                session,
                kept,
                citation_format=self._config.citation_format,
                entity_ids_by_hit=expansion.entity_ids_by_hit,
                device_id=self.device_id,
            )

            warnings = [*outcome.warnings, *expansion.warnings, *provenance_warnings]
            if dropped:
                warnings.append(TEMPORAL_DROP_WARNING)
            # Age, not correctness: these hits are real, but they may describe a file as it was
            # rather than as it is. Only the sources actually returned are examined, so a disabled
            # root that contributed nothing to this answer is not mentioned.
            warnings.extend(
                staleness_warnings(session, [h.provenance.source_id for h in hits if h.provenance])
            )

            context, context_warnings = self._assemble(
                session, query, hits, expansion, flags, moment
            )
            warnings.extend(context_warnings)
            # Deduplicated once, before the log is written, so the stored row and the response the
            # caller saw carry exactly the same warnings - which is what makes a log replayable.
            warnings = _dedupe(warnings)

            counts = {**outcome.candidate_counts, **expansion.counts, "returned": len(hits)}
            latency_ms = int((time.perf_counter() - started) * 1000)
            log_id = write_retrieval_log(
                session,
                query,
                hits=hits,
                candidate_counts=counts,
                latency_ms=latency_ms,
                warnings=warnings,
                config=self._config,
                client=query.client,
                config_version=outcome.config_version,
                embedding_model_id=outcome.embedding_model_id,
            )

            return SearchResult(
                query=query,
                hits=hits,
                related_entities=expansion.related,
                context=context,
                provenance=[hit.provenance for hit in hits],
                candidate_counts=counts,
                latency_ms=latency_ms,
                warnings=warnings,
                retrieval_log_id=log_id,
            )

    # ------------------------------------------------------------------- search internals

    def _expand(
        self,
        session: Session,
        query: SearchQuery,
        outcome: RetrievalOutcome,
        moment: datetime,
    ) -> GraphExpansion:
        """Stage 4 over the *fused* list (``fused_top_k``), which is its documented input."""
        keys: list[HitKey] = [(c.object_type, c.object_id) for c in outcome.fused]
        return expand(
            session,
            self._graph,
            keys,
            config=self._config,
            as_of=query.as_of or moment,
            project_ids=query.project_ids or None,
            enabled=query.expand and self._config.graph_expansion_enabled,
        )

    def _assemble(
        self,
        session: Session,
        query: SearchQuery,
        hits: Sequence[ScoredHit],
        expansion: GraphExpansion,
        flags: dict[UUID, list[str]],
        moment: datetime,
    ) -> tuple[AssembledContext | None, list[str]]:
        """Stage 8. Returns ``(context, warnings)``; ``None`` when the caller did not ask for one."""
        if not query.assemble_context:
            return None, []
        tracks = queries.project_tracks(session)
        project_ids = _project_ids(query, hits)
        inputs = ContextInputs(
            projects=_project_summaries(session, project_ids),
            facts=facts_for_entities(
                session,
                expansion.seed_entity_ids or None,
                as_of=query.as_of or moment,
                project_ids=project_ids or None,
                include_unconfirmed=query.include_unconfirmed,
                limit=25,
            )
            if expansion.seed_entity_ids or project_ids
            else [],
            decisions=_decision_rows(
                session,
                tracks,
                project_ids=project_ids,
                as_of=query.as_of or moment,
                citation_format=self._config.citation_format,
                device_id=self.device_id,
            ),
            hits=list(hits),
            related=list(expansion.related),
            tracks=tracks,
            hit_flags=flags,
        )
        context = assemble_context(inputs, config=self._config)
        warning = track_mix_warning(inputs)
        return context, [warning] if warning else []

    # ---------------------------------------------------------------------------- registry reads

    def list_projects(self, project_ids: Sequence[str] | None = None) -> list[ProjectView]:
        """``GET /v1/projects``. Coverage is MEASURED per call (ADR-0006 honesty)."""
        with self._session() as session:
            return queries.list_projects(session, project_ids)

    def get_project(self, project_id: str) -> ProjectView:
        """``GET /v1/projects/{id}``. Raises :class:`NotFoundError` - an empty view would be a lie."""
        with self._session() as session:
            found = queries.list_projects(session, [project_id])
        if not found:
            raise NotFoundError("No such project.", detail=project_id)
        return found[0]

    def get_entity(
        self,
        entity_id: UUID | str,
        *,
        as_of: datetime | None = None,
        include_related: bool = True,
        fact_limit: int = 50,
    ) -> EntityView:
        """``GET /v1/entities/{id}``: the entity, its facts current at ``as_of``, its provenance."""
        moment = ensure_utc(as_of) if as_of else utc_now()
        with self._session() as session:
            tracks = queries.project_tracks(session)
            view = queries.entity_view(session, entity_id, tracks)
            if view is None:
                raise NotFoundError("No such entity.", detail=str(entity_id))
            facts = facts_for_entities(session, [view.id], as_of=moment, limit=fact_limit)
            view.facts = queries.fact_views(facts, tracks)
            if include_related:
                view.related, view.warnings = self._related_for(session, [view.id], moment, None)
            return view

    def _related_for(
        self,
        session: Session,
        entity_ids: Sequence[UUID],
        moment: datetime,
        project_ids: Sequence[str] | None,
    ) -> tuple[list[RelatedEntity], list[str]]:
        """Graph expansion for an explicit entity list; degrades to ``([], [warning])``."""
        from ..retrieval.expansion import GRAPH_DEGRADED_WARNING, neighbours

        if self._graph is None or not self._config.graph_expansion_enabled or not entity_ids:
            return [], []
        try:
            return (
                neighbours(
                    self._graph,
                    list(entity_ids),
                    config=self._config,
                    as_of=moment,
                    project_ids=project_ids,
                ),
                [],
            )
        except Exception as exc:  # noqa: BLE001 - a graph outage degrades this route too
            logger.warning("gateway.graph_unavailable", error=type(exc).__name__)
            return [], [GRAPH_DEGRADED_WARNING]

    def get_related(
        self,
        entity_ids: Sequence[UUID | str],
        *,
        as_of: datetime | None = None,
        project_ids: Sequence[str] | None = None,
    ) -> RelatedResult:
        """``GET /v1/related`` - expansion on its own, with the same degradation contract."""
        moment = ensure_utc(as_of) if as_of else utc_now()
        ids = [UUID(str(e)) for e in entity_ids]
        with self._session() as session:
            related, warnings = self._related_for(session, ids, moment, project_ids)
        return RelatedResult(
            entity_ids=ids,
            related=related,
            as_of=moment,
            warnings=warnings,
            degraded=bool(warnings),
        )

    def get_decisions(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        include_superseded: bool = False,
        as_of: datetime | None = None,
        limit: int = 50,
    ) -> list[ArtifactView]:
        """``GET /v1/decisions`` - current decision artifacts, superseded ones only on request."""
        with self._session() as session:
            tracks = queries.project_tracks(session)
            return queries.list_decisions(
                session,
                tracks,
                project_ids=project_ids,
                include_superseded=include_superseded,
                as_of=as_of,
                limit=limit,
                citation_format=self._config.citation_format,
                device_id=self.device_id,
            )

    def get_artifact(self, artifact_id: UUID | str) -> ArtifactView:
        """``GET /v1/artifacts/{id}`` - the artifact and both ends of its supersession chain."""
        with self._session() as session:
            tracks = queries.project_tracks(session)
            view = queries.get_artifact(
                session,
                artifact_id,
                tracks,
                citation_format=self._config.citation_format,
                device_id=self.device_id,
            )
        if view is None:
            raise NotFoundError("No such artifact.", detail=str(artifact_id))
        return view

    def get_timeline(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        entity_ids: Sequence[UUID | str] | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        limit: int = 100,
    ) -> list[TimelineEvent]:
        """``GET /v1/timeline`` - facts, artifacts and source events merged (``temporal.md`` §8)."""
        with self._session() as session:
            tracks = queries.project_tracks(session)
            return queries.timeline(
                session,
                tracks,
                project_ids=project_ids,
                entity_ids=[UUID(str(e)) for e in entity_ids] if entity_ids else None,
                from_at=from_at,
                to_at=to_at,
                limit=limit,
            )

    def get_sources(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        root_id: str | None = None,
        uri: str | None = None,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SourceView]:
        """``GET /v1/sources`` - the registry. URIs, hashes, sizes; never file bytes."""
        statuses = (
            tuple(status.value for status in SourceStatus)
            if include_deleted
            else (SourceStatus.ACTIVE.value, SourceStatus.MOVED.value)
        )
        with self._session() as session:
            tracks = queries.project_tracks(session)
            return queries.list_sources(
                session,
                tracks,
                project_ids=project_ids,
                root_id=root_id,
                uri=uri,
                statuses=statuses,
                limit=limit,
                offset=offset,
            )

    def explain(self, object_id: UUID | str, *, object_type: str | None = None) -> ExplainChain:
        """``GET /v1/explain/{id}`` - the plan section J chain, resolved from PostgreSQL alone."""
        with self._session() as session:
            result = explain_chain(session, object_id, object_type=object_type)
            provenance = result.provenance or Provenance(
                device_id=self.device_id, observed_at=utc_now()
            )
            return ExplainChain(
                object_type=_object_type_of(result.object_type),
                object_id=UUID(str(result.object_id)),
                provenance=provenance,
                steps=result.steps,
                citation=provenance.citation() if provenance.source_uri else None,
                explanation=_render_explanation(result.label, result.steps),
                complete=result.complete,
            )

    # ------------------------------------------------------------------------------ state + ops

    def get_current_state(
        self,
        *,
        project_ids: Sequence[str] | None = None,
        as_of: datetime | None = None,
        limit: int = 20,
    ) -> CurrentState:
        """``GET /v1/state`` - "what is true now", with honest coverage (ADR-0006).

        Status, current facts, open tasks, latest decisions, the last ingestion run and coverage,
        grouped by track. A project with nothing ingested still appears, with a coverage note that
        says so - silence would be indistinguishable from "nothing to report".
        """
        moment = ensure_utc(as_of) if as_of else utc_now()
        with self._session() as session:
            tracks = queries.project_tracks(session)
            projects = queries.list_projects(session, project_ids)
            scope = [project.id for project in projects]
            facts = facts_for_entities(
                session, None, as_of=moment, project_ids=scope or None, limit=limit
            )
            state = CurrentState(
                as_of=moment,
                project_ids=scope,
                projects=projects,
                current_facts=queries.fact_views(facts, tracks),
                open_tasks=queries.open_tasks(
                    session,
                    tracks,
                    project_ids=scope or None,
                    as_of=moment,
                    limit=limit,
                    citation_format=self._config.citation_format,
                    device_id=self.device_id,
                ),
                latest_decisions=queries.list_decisions(
                    session,
                    tracks,
                    project_ids=scope or None,
                    as_of=moment,
                    limit=limit,
                    citation_format=self._config.citation_format,
                    device_id=self.device_id,
                ),
                last_ingestion=queries.last_ingestion(session),
                coverage=[p.coverage for p in projects if p.coverage is not None],
                by_track=_by_track(projects),
            )
        if state.last_ingestion is None:
            state.warnings = ["nothing has been ingested yet; this state is empty by fact, not by error"]
        return state

    def metrics_corpus(self) -> dict[str, int]:
        """MEASURED corpus totals for ``/metrics``; the counters themselves live in the API layer."""
        with self._session() as session:
            return queries.corpus_counts(session)

    def health(self) -> HealthReport:
        """``GET /health``: PostgreSQL, Neo4j (as the configured read user) and the embedder.

        Never raises and never leaks: each check's ``detail`` is an exception *type*, not a message,
        so a DSN or a password can never reach the response body (plan section T).
        """
        checks = [self._check_postgres(), self._check_neo4j(), self._check_embedding()]
        required = {"postgres"}
        down = {check.name for check in checks if not check.ok}
        status = "ok" if not down else ("down" if down & required else "degraded")
        return HealthReport(
            status=status,
            checks=checks,
            writes_enabled=self.writes_enabled,
            at=utc_now(),
        )

    def _timed(self, name: str, probe: Callable[[], tuple[bool, str]]) -> ComponentHealth:
        started = time.perf_counter()
        try:
            ok, detail = probe()
        except Exception as exc:  # noqa: BLE001 - a health check reports, it never raises
            ok, detail = False, type(exc).__name__
        return ComponentHealth(
            name=name,
            ok=ok,
            detail=detail,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _check_postgres(self) -> ComponentHealth:
        def probe() -> tuple[bool, str]:
            from sqlalchemy import text as sql_text

            with self._session() as session:
                value = session.execute(sql_text("SELECT 1")).scalar_one()
            return bool(value == 1), "reachable"

        return self._timed("postgres", probe)

    def _check_neo4j(self) -> ComponentHealth:
        def probe() -> tuple[bool, str]:
            if self._graph is None:
                return False, "not configured"
            ok = self._graph.health()
            return ok, "reachable" if ok else "unreachable"

        check = self._timed("neo4j", probe)
        return check

    def _check_embedding(self) -> ComponentHealth:
        def probe() -> tuple[bool, str]:
            if self._embedder is None:
                return False, "not configured"
            ok = self._embedder.health()
            return ok, "reachable" if ok else "unreachable"

        return self._timed("embedding", probe)

    # ---------------------------------------------------------------------------------- writes

    def record_mcp_audit(self, payload: Mapping[str, Any]) -> UUID:
        """``POST /v1/mcp/audit`` - persist one ADR-0008 audit record for the MCP server.

        Not gated by ``GATEWAY_WRITE_ENABLED``: a *refusal* can only occur while writes are off, so
        gating this would drop precisely the records the policy exists to keep. See
        :func:`aimemory.gateway.writes.record_mcp_audit`.
        """
        with self._session() as session:
            return _record_mcp_audit(session, payload)

    def add_episode(
        self,
        *,
        text: str,
        title: str | None = None,
        project_id: str | None = None,
        client: str | None = None,
        occurred_at: datetime | None = None,
    ) -> WriteReceipt:
        """``POST /v1/episodes`` - append a manual/MCP episode (ADR-0008). 403 unless enabled."""
        with self._session() as session:
            return _add_episode(
                session,
                text=text,
                title=title,
                project_id=project_id,
                client=client,
                occurred_at=occurred_at,
                settings=self._settings,
            )

    def record_decision(
        self,
        *,
        title: str,
        body: str,
        project_id: str | None = None,
        supersedes_id: UUID | str | None = None,
        client: str | None = None,
        decided_at: datetime | None = None,
    ) -> WriteReceipt:
        """``POST /v1/decisions`` - append a decision artifact + its episode (ADR-0008)."""
        with self._session() as session:
            return _record_decision(
                session,
                title=title,
                body=body,
                project_id=project_id,
                supersedes_id=supersedes_id,
                client=client,
                decided_at=decided_at,
                settings=self._settings,
            )


# ------------------------------------------------------------------------------------- helpers


def _dedupe(values: Sequence[str]) -> list[str]:
    """Order-preserving unique - a warning repeated by two stages is still one fact."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _hit_flags(hits: Sequence[RankedCandidate]) -> dict[UUID, list[str]]:
    """``source-deleted`` / ``low-trust`` from :class:`HitMetadata`, which ``ScoredHit`` drops."""
    flags: dict[UUID, list[str]] = {}
    for hit in hits:
        marks: list[str] = []
        if hit.metadata.source_status == SourceStatus.DELETED.value:
            marks.append("source-deleted")
        if hit.metadata.trust == Trust.LOW.value:
            marks.append(f"{Trust.LOW.value}-trust")
        if marks:
            flags[hit.object_id] = marks
    return flags


def _project_ids(query: SearchQuery, hits: Sequence[ScoredHit]) -> list[str]:
    """Explicit scoping wins; otherwise the projects the hits actually came from."""
    if query.project_ids:
        return list(query.project_ids)
    found: list[str] = []
    for hit in hits:
        if hit.project_id and hit.project_id not in found:
            found.append(hit.project_id)
    return found


def _project_summaries(session: Session, project_ids: Sequence[str]) -> list[ProjectSummary]:
    if not project_ids:
        return []
    return [
        ProjectSummary(
            project_id=view.id,
            name=view.name,
            track=view.track,
            status=view.status,
            summary=view.summary,
            coverage_note=view.coverage.note if view.coverage else None,
        )
        for view in queries.list_projects(session, project_ids)
    ]


def _decision_rows(
    session: Session,
    tracks: dict[str, str],
    *,
    project_ids: Sequence[str],
    as_of: datetime,
    citation_format: str,
    device_id: str,
    limit: int = 10,
) -> list[DecisionRow]:
    views = queries.list_decisions(
        session,
        tracks,
        project_ids=project_ids or None,
        include_superseded=False,
        as_of=as_of,
        limit=limit,
        citation_format=citation_format,
        device_id=device_id,
    )
    return [
        DecisionRow(
            id=view.id,
            title=view.title,
            body=view.body,
            status=view.status,
            project_id=view.project_id,
            valid_from=view.valid_from,
            supersedes_title=view.supersedes_title,
            supersedes_id=view.supersedes_id,
            citation=view.citation,
            provenance=view.provenance,
            source_status=view.source_status,
        )
        for view in views
    ]


def _by_track(projects: Sequence[ProjectView]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for project in projects:
        grouped.setdefault(project.track, []).append(project.id)
    return grouped


#: ``explain()`` distinguishes ``entity_mention`` from ``entity``; :class:`ObjectType` does not.
#: The precise kind is never lost - it is ``steps[0].kind`` of the returned chain.
_EXPLAIN_OBJECT_TYPES = {
    "fact": ObjectType.FACT,
    "artifact": ObjectType.ARTIFACT,
    "entity": ObjectType.ENTITY,
    "entity_mention": ObjectType.ENTITY,
    "chunk": ObjectType.CHUNK,
    "episode": ObjectType.EPISODE,
    "source": ObjectType.SOURCE,
}


def _object_type_of(kind: str) -> ObjectType:
    return _EXPLAIN_OBJECT_TYPES.get(kind, ObjectType.ENTITY)


def _render_explanation(label: str, steps: Sequence[object]) -> str:
    """A human-readable sentence chain: "X, from episode ..., from version ..., from source ..."."""
    from ..domain.provenance import ProvenanceChainStep

    parts: list[str] = []
    for step in steps:
        if not isinstance(step, ProvenanceChainStep):
            continue
        parts.append(f"{step.kind} {step.label}".strip())
    if not parts:
        return label
    return " <- ".join(parts)
