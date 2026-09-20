"""The twelve tool bodies and the two resources. Owner: A10.

Each handler is a translation, nothing more: contract arguments in, one or two memory-api calls, a
JSON-serializable answer out. The interesting rules - project scoping, the ADR-0005 ``as_of``
predicate, provenance assembly, "logical source URIs, never file bytes" - are all enforced inside the
Gateway, so a handler that forgot one could not produce a violating answer even if it tried.

Two translations are worth naming because they are not one-to-one:

* ``memory.get_related`` takes ``relationship_types`` and ``depth`` (1-2); memory-api's
  ``GET /v1/related`` is a single hop with no predicate filter. So depth 2 is a second round of hops
  issued from the first round's neighbours, and the predicate filter is applied here. Both are stated
  in the returned ``warnings`` when they change what the Gateway would have returned on its own.
* ``memory.get_sources`` and ``memory.explain`` share ``GET /v1/explain/{id}``; per ``tools.json`` the
  first returns the chain (provenance + steps) and the second adds the human-readable sentence.

Known V0.1 limitation, surfaced rather than worked around: **memory-api has no entity-name index.**
``GET /v1/entities/{id}`` takes a UUID, ``/v1/search`` indexes chunks only, and the fact rows returned
by ``/v1/state`` come back with ``subject_entity_id: null``. So ``id_or_name``/``entity`` resolve for
a UUID, are attempted through search for a name, and otherwise return a clear "pass the UUID" error
instead of a guess. See the P11-T01 result (NEEDS_HANDOFF) for the endpoint that would close this.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from api_client import ApiError, MemoryApiClient

__all__ = ["Handlers"]

#: Hard ceiling on what a single read tool will pull back from the Gateway, so one call cannot drag
#: the whole corpus through an MCP session. Below every memory-api ``limit`` maximum.
MAX_TIMELINE_EVENTS = 200
MAX_DECISIONS = 200
MAX_STATE_ITEMS = 50


def _is_uuid(value: str) -> bool:
    try:
        UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _norm(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


class Handlers:
    """Tool bodies bound to one :class:`MemoryApiClient`."""

    def __init__(self, client: MemoryApiClient) -> None:
        self.api = client

    # ================================================================================ resolution ==
    async def resolve_project(self, id_or_name: str) -> dict[str, Any]:
        """Return the project whose id, name or alias matches. Raises :class:`ApiError` otherwise."""
        try:
            return await self.api.get_project(id_or_name)
        except ApiError as exc:
            if exc.status != 404:
                raise
        projects = await self.api.list_projects()
        wanted = _norm(id_or_name)
        for project in projects:
            names = [project.get("id", ""), project.get("name", "")]
            names += list(project.get("aliases") or [])
            if any(_norm(n) == wanted for n in names if n):
                return project
        partial = [p for p in projects if wanted and wanted in _norm(p.get("name") or "")]
        if len(partial) == 1:
            return partial[0]
        raise ApiError(
            f"No project matches {id_or_name!r}. Read memory://projects for the registry.",
            status=404,
            code="not_found",
        )

    async def resolve_entity_id(self, id_or_name: str, *, type_hint: str | None = None) -> str:
        """Resolve an entity reference to a UUID, or raise with an honest explanation."""
        if _is_uuid(id_or_name):
            return str(id_or_name)

        # Best effort: once entities are indexed for retrieval this finds them; today it returns
        # nothing (chunks are the only indexed object type), which is why the error below exists.
        body: dict[str, Any] = {
            "query": id_or_name,
            "object_types": ["entity"],
            "k": 5,
            "assemble_context": False,
            "expand": False,
        }
        if type_hint:
            body["entity_types"] = [type_hint]
        try:
            result = await self.api.search(body)
        except ApiError:
            result = {"hits": []}

        wanted = _norm(id_or_name)
        for hit in result.get("hits") or []:
            if hit.get("object_type") == "entity" and _norm(hit.get("title") or "") == wanted:
                return str(hit["object_id"])
        for hit in result.get("hits") or []:
            if hit.get("object_type") == "entity":
                return str(hit["object_id"])

        raise ApiError(
            f"Could not resolve the entity {id_or_name!r}. V0.1 memory-api has no lookup by entity "
            "name - pass the entity UUID (memory.search hits and memory.get_related neighbours "
            "carry them). See docs/operations/mcp.md.",
            status=404,
            code="entity_unresolved",
        )

    # ====================================================================================== read ==
    async def search(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "query": args["query"],
            "k": int(args.get("k", 10)),
            "expand": bool(args.get("expand", True)),
            "assemble_context": True,
            "client": client_id,
        }
        if args.get("project_ids"):
            body["project_ids"] = list(args["project_ids"])
        if args.get("types"):
            body["object_types"] = list(args["types"])
        if args.get("as_of"):
            body["as_of"] = args["as_of"]
        if args.get("since"):
            body["since"] = args["since"]

        result = await self.api.search(body)
        return {
            "query": result.get("query", args["query"]),
            "hits": result.get("hits", []),
            "related_entities": result.get("related_entities", []),
            "context": result.get("context"),
            "provenance": result.get("provenance", []),
            "candidate_counts": result.get("candidate_counts", {}),
            "latency_ms": result.get("latency_ms"),
            "warnings": result.get("warnings", []),
        }

    async def get_project(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        project = await self.resolve_project(args["id_or_name"])
        project_id = project["id"]
        everything = await self.api.list_projects()
        subprojects = [
            {
                "id": p["id"],
                "name": p.get("name"),
                "status": p.get("status"),
                "track": p.get("track"),
            }
            for p in everything
            if p.get("parent_id") == project_id
        ]
        warnings: list[str] = []
        facts: list[dict[str, Any]] = []
        try:
            state = await self.api.get_state(project_ids=[project_id], limit=MAX_STATE_ITEMS)
            facts = state.get("current_facts", [])
            warnings = state.get("warnings", [])
        except ApiError as exc:
            warnings = [f"Current facts unavailable: {exc.message}"]
        return {
            "project": project,
            "subprojects": subprojects,
            "current_facts": [
                {
                    "id": f.get("id"),
                    "statement": f.get("statement"),
                    "predicate": f.get("predicate"),
                    "subject": f.get("subject_name"),
                    "object": f.get("object_name"),
                    "status": f.get("status"),
                    "valid_from": f.get("valid_from"),
                    "valid_to": f.get("valid_to"),
                    "confidence": f.get("confidence"),
                }
                for f in facts
            ],
            "fact_count": len(facts),
            "coverage": project.get("coverage"),
            "warnings": warnings,
        }

    async def get_entity(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        entity_id = await self.resolve_entity_id(args["id_or_name"], type_hint=args.get("type"))
        entity = await self.api.get_entity(entity_id, include_related=True)
        return {
            "entity": {
                key: entity.get(key)
                for key in (
                    "id",
                    "canonical_name",
                    "normalized_name",
                    "type",
                    "aliases",
                    "summary",
                    "status",
                    "project_id",
                    "track",
                    "engine",
                    "confidence",
                    "first_seen_at",
                    "last_seen_at",
                )
            },
            "facts": entity.get("facts", []),
            "related": entity.get("related", []),
            "provenance": entity.get("provenance"),
            "warnings": entity.get("warnings", []),
        }

    async def get_related(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        entity_id = await self.resolve_entity_id(args["entity"])
        depth = int(args.get("depth", 1))
        wanted = {str(p).upper() for p in (args.get("relationship_types") or [])}
        as_of = args.get("as_of")

        warnings: list[str] = []
        seen: set[str] = {entity_id}
        collected: list[dict[str, Any]] = []
        frontier = [entity_id]

        for hop in range(1, depth + 1):
            if not frontier:
                break
            result = await self.api.get_related(frontier, as_of=as_of)
            warnings.extend(result.get("warnings", []))
            if result.get("degraded"):
                warnings.append("Graph expansion is degraded; the Neo4j projection may be stale.")
            next_frontier: list[str] = []
            for neighbour in result.get("related", []):
                neighbour_id = str(neighbour.get("entity_id"))
                if neighbour_id in seen:
                    continue
                seen.add(neighbour_id)
                next_frontier.append(neighbour_id)
                collected.append({**neighbour, "hops": hop})
            frontier = next_frontier

        if wanted:
            before = len(collected)
            collected = [r for r in collected if str(r.get("predicate", "")).upper() in wanted]
            if before != len(collected):
                warnings.append(
                    f"relationship_types filtered {before - len(collected)} neighbour(s) in the "
                    "MCP layer; the Gateway returns all predicates."
                )
        return {
            "entity_id": entity_id,
            "depth": depth,
            "related": collected,
            "as_of": as_of,
            "warnings": sorted(set(warnings)),
        }

    async def get_decisions(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        project_ids = [args["project_id"]] if args.get("project_id") else None
        decisions = await self.api.get_decisions(
            project_ids=project_ids,
            include_superseded=bool(args.get("include_superseded", False)),
            limit=MAX_DECISIONS,
        )
        return {
            "project_id": args.get("project_id"),
            "include_superseded": bool(args.get("include_superseded", False)),
            "count": len(decisions),
            "decisions": decisions,
        }

    async def get_timeline(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        entity_ids = None
        warnings: list[str] = []
        if args.get("entity"):
            entity_ids = [await self.resolve_entity_id(args["entity"])]
        project_ids = [args["project_id"]] if args.get("project_id") else None
        events = await self.api.get_timeline(
            project_ids=project_ids,
            entity_ids=entity_ids,
            from_at=args.get("from"),
            to_at=args.get("to"),
            limit=MAX_TIMELINE_EVENTS,
        )
        if len(events) == MAX_TIMELINE_EVENTS:
            warnings.append(f"Truncated at {MAX_TIMELINE_EVENTS} events; narrow from/to.")
        return {
            "project_id": args.get("project_id"),
            "entity": args.get("entity"),
            "from": args.get("from"),
            "to": args.get("to"),
            "count": len(events),
            "events": events,
            "warnings": warnings,
        }

    async def get_sources(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        chain = await self._explain_chain(args["object_id"])
        return {
            "object_id": chain.get("object_id"),
            "object_type": chain.get("object_type"),
            "provenance": chain.get("provenance"),
            "steps": chain.get("steps", []),
            "citation": chain.get("citation"),
            "complete": chain.get("complete"),
        }

    async def explain(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        chain = await self._explain_chain(args["object_id"])
        return {
            "object_id": chain.get("object_id"),
            "object_type": chain.get("object_type"),
            "explanation": chain.get("explanation"),
            "provenance": chain.get("provenance"),
            "steps": chain.get("steps", []),
            "citation": chain.get("citation"),
            "evidence_quote": chain.get("evidence_quote"),
            "complete": chain.get("complete"),
        }

    async def _explain_chain(self, object_id: str) -> dict[str, Any]:
        if not _is_uuid(object_id):
            raise ApiError(
                "object_id must be the UUID of a chunk, fact, artifact, episode or source.",
                status=422,
                code="validation_error",
            )
        return await self.api.explain(object_id)

    async def get_artifact(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        artifact_id = args["artifact_id"]
        if not _is_uuid(artifact_id):
            raise ApiError(
                "artifact_id must be a UUID; memory.get_decisions lists them.",
                status=422,
                code="validation_error",
            )
        artifact = await self.api.get_artifact(artifact_id)
        return {
            "artifact": artifact,
            "supersession": {
                "supersedes_id": artifact.get("supersedes_id"),
                "supersedes_title": artifact.get("supersedes_title"),
                "superseded_by_id": artifact.get("superseded_by_id"),
                "superseded_by_title": artifact.get("superseded_by_title"),
                "status": artifact.get("status"),
            },
        }

    async def get_current_state(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        project_id = args["project_id"]
        state = await self.api.get_state(project_ids=[project_id], limit=MAX_STATE_ITEMS)
        return {
            "project_id": project_id,
            "as_of": state.get("as_of"),
            "projects": state.get("projects", []),
            "current_facts": state.get("current_facts", []),
            "open_tasks": state.get("open_tasks", []),
            "latest_decisions": state.get("latest_decisions", []),
            "last_ingestion": state.get("last_ingestion"),
            "coverage": state.get("coverage"),
            "warnings": state.get("warnings", []),
        }

    # ===================================================================================== write ==
    async def add_episode(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"text": args["text"], "client": client_id}
        if args.get("project_id"):
            body["project_id"] = args["project_id"]
        if args.get("occurred_at"):
            body["occurred_at"] = args["occurred_at"]
        receipt = await self.api.add_episode(body)
        return {
            "episode_id": receipt.get("episode_id"),
            "status": "queued" if receipt.get("accepted") else "refused",
            "message": receipt.get("message", ""),
        }

    async def record_decision(self, args: dict[str, Any], *, client_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "title": args["title"],
            "body": args["statement"],
            "project_id": args["project_id"],
            "client": client_id,
        }
        if args.get("supersedes_artifact_id"):
            body["supersedes_id"] = args["supersedes_artifact_id"]
        if args.get("valid_from"):
            body["decided_at"] = args["valid_from"]
        receipt = await self.api.record_decision(body)
        out: dict[str, Any] = {
            "artifact_id": receipt.get("object_id"),
            "episode_id": receipt.get("episode_id"),
            "message": receipt.get("message", ""),
        }
        if args.get("supersedes_artifact_id"):
            out["supersedes_artifact_id"] = args["supersedes_artifact_id"]
        return out

    # ================================================================================= resources ==
    async def resource_projects(self) -> dict[str, Any]:
        projects = await self.api.list_projects()
        return {
            "count": len(projects),
            "projects": [
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "status": p.get("status"),
                    "track": p.get("track"),
                    "parent_id": p.get("parent_id"),
                    "aliases": p.get("aliases", []),
                    "coverage": p.get("coverage"),
                }
                for p in projects
            ],
        }

    async def resource_project_state(self, project_id: str) -> dict[str, Any]:
        return await self.get_current_state({"project_id": project_id}, client_id="resource")
