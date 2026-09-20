"""The structural graph: deterministic, no LLM (ontology.md §6, plan section H; owner A08).

Everything here is true by construction - it comes from the registry, the file system and the
document's own markup - so it is written with ``engine='deterministic'`` and
``extraction_model_id='deterministic:registry-v1'``, and it is available after Tier 0/1, before any
model has run. That is the ADR-0006 promise that the system is useful before the first LLM call:
*"which documents belong to JobLab DE and what do they link to"* is answerable from this layer alone.

What it projects, all read from PostgreSQL:

===========================  ==================================================================
``projects``                 ``(:Project:Entity)`` + ``PART_OF`` for ``parent_id``
``devices``                  ``(:Device)``
``sources`` + roots          ``(:Source)-[:STORED_ON]->(:Device)``
``sources`` INDEX_CONTENT    ``(:Document:Entity)-[:HAS_SOURCE]->(:Source)``, ``BELONGS_TO`` project
``source_text.links``        ``(:Document)-[:LINKS_TO]->(:Document)`` for resolved wikilinks
``episodes``                 ``(:Episode)-[:HAS_SOURCE]->(:Source)``
``entity_mentions``          ``(:Episode)-[:MENTIONS]->(:Entity)``
===========================  ==================================================================

**Deviation from ontology.md §6, and it is deliberate.** §6 gives the ``Document`` node
``id = source_id``, the same value the ``Source`` node uses. Two nodes sharing one ``id`` breaks
:meth:`Neo4jGraphStore.upsert_relationships`, which resolves an edge's endpoints with
``MATCH (n {id: $id})`` and would match both, attaching edges to whichever it saw last. The Document
node therefore uses ``deterministic_id("document", source_id)`` - stable, replayable, and carrying
``source_id`` as a property so the link back is explicit. Reported to A02 rather than hidden.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..common.ids import deterministic_id, normalize_name
from ..common.logging import get_logger
from ..common.time import ensure_utc
from ..domain.enums import EngineKind, EntityType, Predicate
from ..domain.provenance import DETERMINISTIC_MODEL_ID
from ..ontology import Ontology, load_ontology
from ..providers.graph import ProjectionPlan, registry_node, structural_edge

__all__ = [
    "DETERMINISTIC_MODEL_ID",
    "StructuralIndex",
    "document_node_id",
    "rebuild_graph",
    "structural_plan",
]

logger = get_logger(__name__)

_WIKILINK_SUFFIX = re.compile(r"[#|].*$")


def document_node_id(source_id: UUID | str) -> UUID:
    """The ``:Document`` node id for a source. See the module docstring for why it is not ``source_id``."""
    return deterministic_id("document", str(source_id))


@dataclass(slots=True)
class StructuralIndex:
    """Lookups the semantic layer and the rebuild driver both need."""

    project_nodes: dict[str, str] = field(default_factory=dict)  # slug -> node id
    project_labels: dict[str, EntityType] = field(default_factory=dict)
    document_nodes: dict[str, str] = field(default_factory=dict)  # source_id -> document node id
    labels: dict[str, EntityType] = field(default_factory=dict)  # node id -> label
    unresolved_links: int = 0


def structural_plan(
    session: Session,
    *,
    ontology: Ontology | None = None,
    project_id: str | None = None,
) -> tuple[ProjectionPlan, StructuralIndex]:
    """Build the whole deterministic layer. Pure read; nothing is written here."""
    onto = ontology or load_ontology()
    plan = ProjectionPlan()
    index = StructuralIndex()

    _projects(session, plan, index, onto, project_id)
    _devices(session, plan, index)
    _sources_and_documents(session, plan, index, onto, project_id)
    _links(session, plan, index, onto)
    _episodes(session, plan, index, onto, project_id)
    _mentions(session, plan, index, onto)

    logger.info("structural.plan_built", **plan.counts, unresolved_links=index.unresolved_links)
    return plan, index


# --------------------------------------------------------------------------------------------------
# Layers
# --------------------------------------------------------------------------------------------------


def _projects(
    session: Session,
    plan: ProjectionPlan,
    index: StructuralIndex,
    onto: Ontology,
    project_id: str | None,
) -> None:
    """One ``:Project`` per registry project, keyed on its Tier 0 entity row when that exists.

    The Tier 0 seed (ADR-0014 rule 3) writes an ``entities`` row per project. Preferring it keeps the
    project as *one* node instead of a registry node and an entity node that never meet.
    """
    rows = session.execute(
        text(
            """
            SELECT p.id, p.name, p.status, p.parent_id, p.updated_at,
                   e.id AS entity_id, e.summary AS entity_summary
              FROM projects p
              LEFT JOIN entities e
                     ON e.project_id = p.id
                    AND e.type IN ('Project', 'SubProject')
                    AND e.merged_into_id IS NULL
             WHERE (CAST(:pid AS text) IS NULL OR p.id = :pid OR p.parent_id = :pid)
             ORDER BY p.id
            """
        ),
        {"pid": project_id},
    ).mappings().all()

    for row in rows:
        slug = str(row["id"])
        node_id = str(row["entity_id"] or slug)
        entity_type = EntityType.SUB_PROJECT if row["parent_id"] else EntityType.PROJECT
        index.project_nodes[slug] = node_id
        index.project_labels[slug] = entity_type
        index.labels[node_id] = entity_type
        plan.nodes.append(
            registry_node(
                node_id,
                entity_type,
                str(row["name"]),
                observed_at=ensure_utc(row["updated_at"]),
                project_id=slug,
                status=row["status"],
                summary=row["entity_summary"],
                parent_id=row["parent_id"],
                extraction_model_id=DETERMINISTIC_MODEL_ID,
            )
        )

    for row in rows:
        parent = row["parent_id"]
        if not parent or str(parent) not in index.project_nodes:
            continue
        child_id = index.project_nodes[str(row["id"])]
        edge = structural_edge(
            Predicate.PART_OF,
            from_id=child_id,
            to_id=index.project_nodes[str(parent)],
            from_label=EntityType.SUB_PROJECT,
            to_label=index.project_labels[str(parent)],
            observed_at=ensure_utc(row["updated_at"]),
            edge_id=f"part_of:{row['id']}",
            ontology=onto,
        )
        if edge is not None:
            plan.relationships.append(edge)


def _devices(session: Session, plan: ProjectionPlan, index: StructuralIndex) -> None:
    rows = session.execute(
        text("SELECT id, label, os, created_at FROM devices ORDER BY id")
    ).mappings().all()
    for row in rows:
        node_id = str(row["id"])
        index.labels[node_id] = EntityType.DEVICE
        plan.nodes.append(
            registry_node(
                node_id,
                EntityType.DEVICE,
                str(row["label"]),
                observed_at=ensure_utc(row["created_at"]),
                os=row["os"],
                extraction_model_id=DETERMINISTIC_MODEL_ID,
            )
        )


def _sources_and_documents(
    session: Session,
    plan: ProjectionPlan,
    index: StructuralIndex,
    onto: Ontology,
    project_id: str | None,
) -> None:
    rows = session.execute(
        text(
            """
            SELECT s.id, s.uri, s.relative_path, s.project_id, s.policy, s.status, s.kind,
                   s.origin, s.trust, s.secret_suspected, s.last_seen_at, s.root_id,
                   r.device_id, v.id AS version_id, v.content_hash
              FROM sources s
              JOIN source_roots r ON r.root_id = s.root_id
              LEFT JOIN source_versions v ON v.id = s.current_version_id
             WHERE (CAST(:pid AS text) IS NULL OR s.project_id = :pid)
             ORDER BY s.uri
            """
        ),
        {"pid": project_id},
    ).mappings().all()

    for row in rows:
        source_id = str(row["id"])
        observed_at = ensure_utc(row["last_seen_at"])
        index.labels[source_id] = EntityType.SOURCE
        plan.nodes.append(
            registry_node(
                source_id,
                EntityType.SOURCE,
                str(row["relative_path"]),
                observed_at=observed_at,
                project_id=row["project_id"],
                status=row["status"],
                uri=row["uri"],
                root_id=row["root_id"],
                policy=row["policy"],
                origin=row["origin"],
                trust=row["trust"],
                secret_suspected=bool(row["secret_suspected"]),
                source_hash=row["content_hash"],
                extraction_model_id=DETERMINISTIC_MODEL_ID,
            )
        )

        device_id = str(row["device_id"])
        if device_id in index.labels:
            edge = structural_edge(
                Predicate.STORED_ON,
                from_id=source_id,
                to_id=device_id,
                from_label=EntityType.SOURCE,
                to_label=EntityType.DEVICE,
                observed_at=observed_at,
                edge_id=f"stored_on:{source_id}",
                ontology=onto,
            )
            if edge is not None:
                plan.relationships.append(edge)

        if str(row["policy"]) not in ("INDEX_CONTENT", "MIRROR"):
            continue

        doc_id = str(document_node_id(source_id))
        index.document_nodes[source_id] = doc_id
        index.labels[doc_id] = EntityType.DOCUMENT
        plan.nodes.append(
            registry_node(
                doc_id,
                EntityType.DOCUMENT,
                str(row["relative_path"]),
                observed_at=observed_at,
                project_id=row["project_id"],
                source_id=source_id,
                uri=row["uri"],
                status=row["status"],
                extraction_model_id=DETERMINISTIC_MODEL_ID,
            )
        )
        edge = structural_edge(
            Predicate.HAS_SOURCE,
            from_id=doc_id,
            to_id=source_id,
            from_label=EntityType.DOCUMENT,
            to_label=EntityType.SOURCE,
            observed_at=observed_at,
            edge_id=f"has_source:{doc_id}",
            ontology=onto,
        )
        if edge is not None:
            plan.relationships.append(edge)

        slug = row["project_id"]
        if slug and str(slug) in index.project_nodes:
            edge = structural_edge(
                Predicate.BELONGS_TO,
                from_id=doc_id,
                to_id=index.project_nodes[str(slug)],
                from_label=EntityType.DOCUMENT,
                to_label=index.project_labels[str(slug)],
                observed_at=observed_at,
                edge_id=f"belongs_to:{doc_id}",
                ontology=onto,
            )
            if edge is not None:
                plan.relationships.append(edge)


def _link_targets(session: Session) -> dict[tuple[str, str], str]:
    """``(root_id, normalized target) -> source_id`` for wikilink resolution.

    Obsidian links name a note by its title or path stem, so both are indexed. Resolution stays
    inside one root: a ``[[README]]`` in the vault must not bind to a README in a code repository.
    """
    rows = session.execute(
        text("SELECT id, root_id, relative_path FROM sources WHERE status <> 'deleted'")
    ).mappings().all()
    index: dict[tuple[str, str], str] = {}
    for row in rows:
        relative = str(row["relative_path"])
        root = str(row["root_id"])
        stem = relative.rsplit("/", 1)[-1]
        stem_no_ext = stem.rsplit(".", 1)[0] if "." in stem else stem
        for key in (relative, relative.rsplit(".", 1)[0], stem, stem_no_ext):
            normalized = normalize_name(key)
            if normalized:
                index.setdefault((root, normalized), str(row["id"]))
    return index


def _links(
    session: Session, plan: ProjectionPlan, index: StructuralIndex, onto: Ontology
) -> None:
    targets = _link_targets(session)
    rows = session.execute(
        text(
            """
            SELECT s.id AS source_id, s.root_id, t.links, s.last_seen_at
              FROM source_text t
              JOIN source_versions v ON v.id = t.version_id AND v.is_current
              JOIN sources s ON s.id = v.source_id
             WHERE array_length(t.links, 1) > 0
            """
        )
    ).mappings().all()

    for row in rows:
        from_doc = index.document_nodes.get(str(row["source_id"]))
        if from_doc is None:
            continue
        observed_at = ensure_utc(row["last_seen_at"])
        for link in row["links"] or []:
            cleaned = _WIKILINK_SUFFIX.sub("", str(link)).strip()
            normalized = normalize_name(cleaned)
            if not normalized:
                continue
            target_source = targets.get((str(row["root_id"]), normalized))
            to_doc = index.document_nodes.get(target_source) if target_source else None
            if to_doc is None or to_doc == from_doc:
                index.unresolved_links += 1
                continue
            edge = structural_edge(
                Predicate.LINKS_TO,
                from_id=from_doc,
                to_id=to_doc,
                from_label=EntityType.DOCUMENT,
                to_label=EntityType.DOCUMENT,
                observed_at=observed_at,
                edge_id=f"links_to:{from_doc}:{to_doc}",
                ontology=onto,
                link_text=cleaned[:120],
            )
            if edge is not None:
                plan.relationships.append(edge)


def _episodes(
    session: Session,
    plan: ProjectionPlan,
    index: StructuralIndex,
    onto: Ontology,
    project_id: str | None,
) -> None:
    rows = session.execute(
        text(
            """
            SELECT id, type, title, project_id, source_id, observed_at, status, engine
              FROM episodes
             WHERE (CAST(:pid AS text) IS NULL OR project_id = :pid)
             ORDER BY observed_at
            """
        ),
        {"pid": project_id},
    ).mappings().all()
    for row in rows:
        node_id = str(row["id"])
        observed_at = ensure_utc(row["observed_at"])
        index.labels[node_id] = EntityType.EPISODE
        plan.nodes.append(
            registry_node(
                node_id,
                EntityType.EPISODE,
                str(row["title"] or row["type"]),
                observed_at=observed_at,
                engine=EngineKind(row["engine"]) if row["engine"] else EngineKind.DETERMINISTIC,
                project_id=row["project_id"],
                status=row["status"],
                episode_type=row["type"],
                source_id=str(row["source_id"]) if row["source_id"] else None,
            )
        )
        if row["source_id"] and str(row["source_id"]) in index.labels:
            edge = structural_edge(
                Predicate.HAS_SOURCE,
                from_id=node_id,
                to_id=str(row["source_id"]),
                from_label=EntityType.EPISODE,
                to_label=EntityType.SOURCE,
                observed_at=observed_at,
                edge_id=f"has_source:{node_id}",
                ontology=onto,
            )
            if edge is not None:
                plan.relationships.append(edge)


def _mentions(
    session: Session, plan: ProjectionPlan, index: StructuralIndex, onto: Ontology
) -> None:
    """``entity_mentions`` -> ``(:Episode)-[:MENTIONS]->(:Entity)``, one edge per pair."""
    rows = session.execute(
        text(
            """
            SELECT DISTINCT m.episode_id, m.entity_id, e.type, min(m.observed_at) AS observed_at
              FROM entity_mentions m
              JOIN entities e ON e.id = m.entity_id
             WHERE e.merged_into_id IS NULL
             GROUP BY m.episode_id, m.entity_id, e.type
            """
        )
    ).mappings().all()
    for row in rows:
        episode_id = str(row["episode_id"])
        entity_id = str(row["entity_id"])
        if episode_id not in index.labels:
            continue
        edge = structural_edge(
            Predicate.MENTIONS,
            from_id=episode_id,
            to_id=entity_id,
            from_label=EntityType.EPISODE,
            to_label=EntityType(row["type"]),
            observed_at=ensure_utc(row["observed_at"]),
            edge_id=f"mentions:{episode_id}:{entity_id}",
            ontology=onto,
        )
        if edge is not None:
            plan.relationships.append(edge)


def structural_counts(plan: ProjectionPlan) -> Mapping[str, Any]:
    return plan.counts


def rebuild_graph(**kwargs: Any) -> dict[str, Any]:
    """``aimemory-ingest rebuild-graph``'s entry point. See :func:`aimemory.knowledge.rebuild.rebuild_graph`.

    The implementation lives in :mod:`aimemory.knowledge.rebuild` because it drives *both* layers -
    this deterministic one and the engine-produced one in :mod:`aimemory.knowledge.semantic` - and a
    driver importing ``structural`` while ``structural`` imports the driver is a cycle. The CLI has
    imported ``knowledge.structural.rebuild_graph`` since P6, so the name stays here, resolved lazily.
    """
    from .rebuild import rebuild_graph as _impl  # noqa: PLC0415 - deliberate: breaks the import cycle

    return _impl(**kwargs)
