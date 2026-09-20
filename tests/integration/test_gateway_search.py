"""P10-T01 (A09), integration: the Gateway over a real PostgreSQL (and a simulated Neo4j outage).

What only a database can prove, and what this module therefore asserts:

* every returned hit carries a resolved ``[PROV]`` stamp and a rendered citation (the plan's
  "provenance completeness 100 %" criterion);
* the assembled context is ordered, budgeted, cited and track-labelled;
* **one ``retrieval_logs`` row per query**, with the counts, the result ids and the warnings that
  make an ADR-0010 gold-set comparison replayable;
* **Neo4j down -> vector+keyword with a warning, never a failed query** (plan section Y), simulated
  with a graph double whose calls raise exactly as an unreachable Bolt endpoint does;
* the ADR-0008 write gate: refused by default, append-only when enabled.

The corpus is seeded inside ``pg_session``'s transaction and rolled back at teardown; no real source
root is touched and the ``retrieval_logs`` rows written here never survive the test.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
import sqlalchemy as sa
from aimemory.common.config import Settings
from aimemory.common.errors import NotFoundError, WriteDisabledError
from aimemory.domain.models import EmbeddingModel
from aimemory.domain.ports import EmbeddingResult
from aimemory.domain.retrieval import RetrievalConfig, SearchQuery
from aimemory.gateway import Gateway
from aimemory.retrieval.candidates import vector_literal
from aimemory.retrieval.expansion import GRAPH_DEGRADED_WARNING
from sqlalchemy.orm import Session

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("postgres_available")]

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
DEVICE_ID = "local-development-machine"
MODEL_ID = "minilm-l6-v2-384"
DIM = 384
BIZ, RES = "t-p10-biz", "t-p10-res"
CONFIG = RetrievalConfig(
    graph_relationship_types=["USES", "DEPENDS_ON", "RELATED_TO"],
    graph_max_nodes=25,
    block_order=[
        "project_summary",
        "current_facts",
        "decisions",
        "evidence_chunks",
        "related_entities",
    ],
)


def _id(label: str) -> UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"aimemory-test:p10-gateway:{label}")


def _unit_vector(angle_index: int) -> list[float]:
    angle = angle_index * math.pi / 180.0
    vector = [0.0] * DIM
    vector[0] = math.cos(angle)
    vector[1] = math.sin(angle)
    return vector


def _exec(session: Session, sql: str, params: dict[str, Any] | None = None) -> None:
    session.execute(sa.text(sql), params or {})


class StubEmbedder:
    """Deterministic 384-d query vectors - the SQL is under test here, not the model."""

    def __init__(self, angle: int = 0) -> None:
        self.angle = angle

    @property
    def dimensions(self) -> int:
        return DIM

    def embed(self, texts):  # pragma: no cover - the Gateway only embeds the query
        return EmbeddingResult(vectors=[], model_id=MODEL_ID, dimension=DIM)

    def embed_query(self, text: str) -> list[float]:
        return _unit_vector(self.angle)

    def model_identity(self) -> EmbeddingModel:
        return EmbeddingModel(
            id=MODEL_ID, name="sentence-transformers/all-MiniLM-L6-v2", dimension=DIM
        )

    def health(self) -> bool:
        return True


class DownGraph:
    """A Neo4j double that is unreachable - every call raises, as the driver does on an outage."""

    def query(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        raise ConnectionError("Unable to retrieve routing information")

    def health(self) -> bool:
        return False


class UpGraph:
    """A Neo4j double that answers one neighbour, so ``entity_linked`` has something to link to."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def query(self, cypher: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append(parameters or {})
        return list(self.rows)

    def health(self) -> bool:
        return True


@dataclass
class Corpus:
    chunks: dict[str, UUID]
    artifacts: dict[str, UUID]
    entities: dict[str, UUID]
    facts: dict[str, UUID]
    sources: dict[str, UUID]
    episode_id: UUID


def _seed_source(session: Session, key: str, *, project_id: str, trust: str = "high") -> tuple[UUID, UUID]:
    source_id, version_id = _id(f"source:{key}"), _id(f"version:{key}")
    _exec(
        session,
        """
        INSERT INTO sources (id, uri, root_id, relative_path, project_id, kind, media_type,
                             policy, status, trust, secret_suspected, origin)
        VALUES (:id, :uri, 't-p10-root', :rel, :project, 'file', 'text/markdown',
                'INDEX_CONTENT', 'active', :trust, false, 'internal')
        """,
        {
            "id": source_id,
            "uri": f"vault://p10/{key}.md",
            "rel": f"p10/{key}.md",
            "project": project_id,
            "trust": trust,
        },
    )
    _exec(
        session,
        """
        INSERT INTO source_versions (id, source_id, content_hash, size_bytes, observed_at,
                                     change_type, ingestion_run_id)
        VALUES (:id, :source_id, :hash, 100, :observed_at, 'new', :run)
        """,
        {
            "id": version_id,
            "source_id": source_id,
            "hash": f"sha256:{_id('content:' + key).hex * 2}",
            "observed_at": NOW,
            "run": _id("run"),
        },
    )
    _exec(
        session,
        "UPDATE sources SET current_version_id = :v WHERE id = :s",
        {"v": version_id, "s": source_id},
    )
    return source_id, version_id


def _seed_chunk(
    session: Session, key: str, *, source_id: UUID, version_id: UUID, project_id: str,
    text: str, angle: int,
) -> UUID:
    chunk_id = _id(f"chunk:{key}")
    text_hash = f"sha256:{_id('hash:' + key).hex}"
    _exec(
        session,
        """
        INSERT INTO chunks (id, version_id, source_id, project_id, ordinal, text, text_hash,
                            heading_path, char_start, char_end, token_count)
        VALUES (:id, :version_id, :source_id, :project, 0, :text, :hash,
                ARRAY['Design','Retrieval'], 0, :end, :tokens)
        """,
        {
            "id": chunk_id,
            "version_id": version_id,
            "source_id": source_id,
            "project": project_id,
            "text": text,
            "hash": text_hash,
            "end": len(text),
            "tokens": max(1, len(text) // 4),
        },
    )
    _exec(
        session,
        """
        INSERT INTO embeddings (id, object_type, object_id, text_hash, model_id, dimension, vector)
        VALUES (:id, 'chunk', :object_id, :hash, :model, :dim, CAST(:vector AS vector))
        """,
        {
            "id": _id(f"embedding:{key}"),
            "object_id": chunk_id,
            "hash": text_hash,
            "model": MODEL_ID,
            "dim": DIM,
            "vector": vector_literal(_unit_vector(angle)),
        },
    )
    return chunk_id


@pytest.fixture()
def corpus(pg_session: Session) -> Corpus:
    """Two projects on two tracks, an entity mentioned by a chunk, a decision, and a current fact."""
    session = pg_session
    _exec(
        session,
        "INSERT INTO projects (id, name, track, status, summary) VALUES "
        "(:a, 'P10 Business', 'business', 'active', 'The pilot.'), "
        "(:b, 'P10 Research', 'research', 'active', 'The study.')",
        {"a": BIZ, "b": RES},
    )
    _exec(
        session,
        "INSERT INTO source_roots (root_id, scheme, label, container_path, device_id, "
        "default_project_id, enabled) VALUES ('t-p10-root', 'vault', 't-p10-root', "
        "'/sources/vault', :device, :project, true)",
        {"device": DEVICE_ID, "project": BIZ},
    )
    _exec(
        session,
        "INSERT INTO ingestion_runs (id, root_id, tier, trigger, status, started_at, finished_at) "
        "VALUES (:id, 't-p10-root', 2, 'cli', 'completed', :started, :finished)",
        {"id": _id("run"), "started": NOW - timedelta(hours=2), "finished": NOW - timedelta(hours=1)},
    )

    sources: dict[str, UUID] = {}
    chunks: dict[str, UUID] = {}
    for key, project, angle, text in (
        ("near", BIZ, 0, "The retrieval pipeline fuses pgvector HNSW candidates with tsvector hits."),
        ("mid", BIZ, 25, "Embeddings are reused by text hash so a re-scan costs nothing."),
        ("res", RES, 3, "The research track studies the retrieval pipeline and pgvector recall."),
    ):
        source_id, version_id = _seed_source(session, key, project_id=project)
        sources[key] = source_id
        chunks[key] = _seed_chunk(
            session, key, source_id=source_id, version_id=version_id, project_id=project,
            text=text, angle=angle,
        )

    episode_id = _id("episode")
    _exec(
        session,
        """
        INSERT INTO episodes (id, type, source_id, version_id, project_id, title, body,
                              observed_at, status, tier)
        VALUES (:id, 'document', :source_id, :version_id, :project, 'Near note', 'body',
                :observed_at, 'extracted', 2)
        """,
        {
            "id": episode_id,
            "source_id": sources["near"],
            "version_id": _id("version:near"),
            "project": BIZ,
            "observed_at": NOW,
        },
    )

    entities = {"pgvector": _id("entity:pgvector"), "postgres": _id("entity:postgres")}
    _exec(
        session,
        "INSERT INTO entities (id, type, canonical_name, normalized_name, project_id, engine) "
        "VALUES (:a, 'Technology', 'pgvector', 'pgvector', :project, 'native'), "
        "(:b, 'Technology', 'PostgreSQL', 'postgresql', :project, 'native')",
        {"a": entities["pgvector"], "b": entities["postgres"], "project": BIZ},
    )
    _exec(
        session,
        """
        INSERT INTO entity_mentions (id, entity_id, episode_id, chunk_id, surface_form, source_id,
                                     source_uri, source_hash, source_version, project_id, device_id,
                                     observed_at, valid_from, extraction_model_id, heading_path)
        VALUES (:id, :entity, :episode, :chunk, 'pgvector', :source_id, :uri, :hash, :version,
                :project, :device, :observed_at, :observed_at, 'deterministic:registry-v1',
                ARRAY['Design'])
        """,
        {
            "id": _id("mention:pgvector"),
            "entity": entities["pgvector"],
            "episode": episode_id,
            "chunk": chunks["near"],
            "source_id": sources["near"],
            "uri": "vault://p10/near.md",
            "hash": f"sha256:{_id('content:near').hex * 2}",
            "version": _id("version:near"),
            "project": BIZ,
            "device": DEVICE_ID,
            "observed_at": NOW,
        },
    )
    _exec(
        session,
        """
        INSERT INTO entity_mentions (id, entity_id, episode_id, chunk_id, surface_form, source_id,
                                     source_uri, source_hash, source_version, project_id, device_id,
                                     observed_at, valid_from, extraction_model_id, heading_path)
        VALUES (:id, :entity, :episode, :chunk, 'PostgreSQL', :source_id, :uri, :hash, :version,
                :project, :device, :observed_at, :observed_at, 'deterministic:registry-v1',
                ARRAY['Design'])
        """,
        {
            "id": _id("mention:postgres"),
            "entity": entities["postgres"],
            "episode": episode_id,
            "chunk": chunks["mid"],
            "source_id": sources["mid"],
            "uri": "vault://p10/mid.md",
            "hash": f"sha256:{_id('content:mid').hex * 2}",
            "version": _id("version:mid"),
            "project": BIZ,
            "device": DEVICE_ID,
            "observed_at": NOW,
        },
    )
    return _seed_knowledge(session, sources, chunks, entities, episode_id)


def _seed_knowledge(
    session: Session,
    sources: dict[str, UUID],
    chunks: dict[str, UUID],
    entities: dict[str, UUID],
    episode_id: UUID,
) -> Corpus:
    """A current fact, a current decision (superseding an older one) and an open task."""
    facts = {"uses": _id("fact:uses")}
    _exec(
        session,
        """
        INSERT INTO facts (id, subject_entity_id, predicate, object_entity_id, statement,
                           valid_from, observed_at, status, source_status, project_id, engine,
                           source_id, source_uri, source_hash, source_version, device_id,
                           extraction_model_id, episode_id)
        VALUES (:id, :subject, 'DEPENDS_ON', :object, 'pgvector depends on PostgreSQL',
                :valid_from, :observed_at, 'current', 'active', :project, 'native',
                :source_id, :uri, :hash, :version, :device, 'deterministic:registry-v1', :episode)
        """,
        {
            "id": facts["uses"],
            "subject": entities["pgvector"],
            "object": entities["postgres"],
            "valid_from": NOW - timedelta(days=10),
            "observed_at": NOW - timedelta(days=10),
            "project": BIZ,
            "source_id": sources["near"],
            "uri": "vault://p10/near.md",
            "hash": f"sha256:{_id('content:near').hex * 2}",
            "version": _id("version:near"),
            "device": DEVICE_ID,
            "episode": episode_id,
        },
    )

    artifacts: dict[str, UUID] = {}
    for key, artifact_type, title, body, status, valid_from, valid_to, supersedes in (
        ("old", "decision", "Use weighted score blending",
         "Blend cosine and ts_rank_cd scores for the retrieval pipeline.",
         "superseded", NOW - timedelta(days=200), NOW - timedelta(days=30), None),
        ("current", "decision", "Use RRF for hybrid retrieval",
         "Reciprocal Rank Fusion with k=60 combines pgvector HNSW and tsvector candidates.",
         "current", NOW - timedelta(days=30), None, _id("artifact:old")),
        ("task", "task", "Wire the Gateway into memory-api",
         "Expose /v1/search and friends over REST.", "proposed", NOW - timedelta(days=5), None, None),
    ):
        artifacts[key] = _id(f"artifact:{key}")
        _exec(
            session,
            """
            INSERT INTO knowledge_artifacts (id, type, title, body, project_id, current_status,
                                             valid_from, valid_to, supersedes_id, engine,
                                             source_status, source_id, source_uri, source_hash,
                                             source_version, device_id, observed_at,
                                             extraction_model_id, episode_id)
            VALUES (:id, :type, :title, :body, :project, :status, :valid_from, :valid_to,
                    :supersedes, 'native', 'active', :source_id, :uri, :hash, :version, :device,
                    :observed_at, 'deterministic:registry-v1', :episode)
            """,
            {
                "id": artifacts[key],
                "type": artifact_type,
                "title": title,
                "body": body,
                "project": BIZ,
                "status": status,
                "valid_from": valid_from,
                "valid_to": valid_to,
                "supersedes": supersedes,
                "source_id": sources["near"],
                "uri": "vault://p10/near.md",
                "hash": f"sha256:{_id('content:near').hex * 2}",
                "version": _id("version:near"),
                "device": DEVICE_ID,
                "observed_at": valid_from,
                "episode": episode_id,
            },
        )
    _exec(
        session,
        "UPDATE knowledge_artifacts SET superseded_by_id = :new WHERE id = :old",
        {"new": artifacts["current"], "old": artifacts["old"]},
    )
    _exec(
        session,
        "INSERT INTO artifact_entities (artifact_id, entity_id, role) VALUES (:a, :e, 'mentions')",
        {"a": artifacts["current"], "e": entities["pgvector"]},
    )
    session.flush()
    return Corpus(
        chunks=chunks,
        artifacts=artifacts,
        entities=entities,
        facts=facts,
        sources=sources,
        episode_id=episode_id,
    )


def _neighbour_row(entity_id: UUID, name: str) -> dict[str, Any]:
    return {
        "id": str(entity_id),
        "name": name,
        "type": "Technology",
        "labels": ["Technology", "Entity"],
        "project_id": BIZ,
        "predicate": "DEPENDS_ON",
        "fact_id": str(_id("fact:uses")),
        "confidence": 1.0,
        "valid_from": (NOW - timedelta(days=10)).isoformat(),
        "valid_to": None,
        "outgoing": True,
    }


def _gateway(session: Session, *, graph: Any = None, writes: bool = False) -> Gateway:
    @contextmanager
    def scope() -> Iterator[Session]:
        yield session  # the test's transaction; rolled back by the pg_session fixture

    settings = Settings()
    settings.gateway.write_enabled = writes
    return Gateway(
        scope, graph=graph, embedder=StubEmbedder(), config=CONFIG, settings=settings
    )


@pytest.fixture()
def graph_up(corpus: Corpus) -> UpGraph:
    return UpGraph([_neighbour_row(corpus.entities["postgres"], "PostgreSQL")])


def _search(gateway: Gateway, **kwargs: Any):
    """Scoped to the seeded projects by default.

    The dev database is shared with the real Tier-1 ingest (A07a), so an unscoped query here would
    assert against whatever happens to be ingested at the moment the suite runs. Scoping keeps every
    assertion about rows this test created - and exercises the project filter while it is at it.
    """
    kwargs.setdefault("project_ids", [BIZ])
    return gateway.search(SearchQuery(query="retrieval pipeline pgvector", **kwargs), now=NOW)


# --------------------------------------------------------------------------- the happy path


def test_every_returned_hit_carries_provenance_and_a_citation(
    pg_session: Session, corpus: Corpus, graph_up: UpGraph
) -> None:
    result = _search(_gateway(pg_session, graph=graph_up))

    assert result.hits, "the seeded corpus answers this query"
    assert len(result.provenance) == len(result.hits)
    for hit in result.hits:
        assert hit.provenance.source_id is not None
        assert hit.provenance.source_version is not None
        assert hit.provenance.is_complete, "provenance completeness is 100 % on returned evidence"
        assert hit.citation.startswith("[vault://p10/")
        assert "@" in hit.citation


def test_ranks_are_dense_and_scores_are_explainable(
    pg_session: Session, corpus: Corpus, graph_up: UpGraph
) -> None:
    result = _search(_gateway(pg_session, graph=graph_up), project_ids=[BIZ])

    assert [hit.rank for hit in result.hits] == list(range(1, len(result.hits) + 1))
    for hit in result.hits:
        # ``boosts`` is a complete additive decomposition of the score (base components included),
        # so a stored retrieval_logs row explains the ranking without re-running the query.
        assert hit.score == pytest.approx(sum(hit.boosts.values()))
        assert 0.0 <= hit.score <= 2.0, "scores are calibrated relevance, not RRF magnitudes"
    assert any("project_match" in hit.boosts for hit in result.hits)


def test_graph_expansion_links_a_hit_and_returns_the_neighbour(
    pg_session: Session, corpus: Corpus, graph_up: UpGraph
) -> None:
    result = _search(_gateway(pg_session, graph=graph_up))

    # Asserted first: a degraded run retrieves a different candidate set, and the failure should say
    # "we degraded" rather than the confusing "nothing was entity-linked" two lines later.
    assert result.warnings == []
    assert [item.name for item in result.related_entities] == ["PostgreSQL"]
    linked = [hit for hit in result.hits if "entity_linked" in hit.boosts]
    assert linked, "the chunk mentioning PostgreSQL shares an entity with the expansion"
    assert corpus.entities["postgres"] in linked[0].entity_ids
    assert linked[0].object_id == corpus.chunks["mid"]


def test_the_assembled_context_is_ordered_budgeted_and_cited(
    pg_session: Session, corpus: Corpus, graph_up: UpGraph
) -> None:
    result = _search(_gateway(pg_session, graph=graph_up), project_ids=[BIZ])

    context = result.context
    assert context is not None
    kinds = [block.kind for block in context.blocks]
    assert kinds == [kind for kind in CONFIG.block_order if kind in kinds], "block_order is kept"
    assert "evidence_chunks" in kinds
    assert context.tokens_used <= context.token_budget
    assert context.citations
    decisions = [block for block in context.blocks if block.kind == "decisions"]
    assert decisions and "Use RRF for hybrid retrieval" in decisions[0].text
    assert "supersedes: Use weighted score blending" in decisions[0].text


def test_business_and_research_hits_are_labelled_not_merged(
    pg_session: Session, corpus: Corpus, graph_up: UpGraph
) -> None:
    """Plan section Q: a two-track answer is labelled and flagged, never quietly interleaved."""
    result = _search(_gateway(pg_session, graph=graph_up), project_ids=[BIZ, RES])

    assert result.context is not None
    evidence = next(b for b in result.context.blocks if b.kind == "evidence_chunks")
    assert {"track:business", "track:research"} <= set(evidence.flags)
    assert "[business track]" in evidence.text
    assert "[research track]" in evidence.text
    assert any("track" in warning for warning in result.warnings)


# ------------------------------------------------------------------ degradation (plan section Y)


def test_neo4j_down_degrades_to_vector_plus_keyword_with_a_warning(
    pg_session: Session, corpus: Corpus
) -> None:
    """The plan's failure test, simulated end to end: the graph is unreachable, the query still
    answers, the caller is told, and no ``entity_linked`` boost is invented."""
    result = _search(_gateway(pg_session, graph=DownGraph()))

    assert GRAPH_DEGRADED_WARNING in result.warnings
    assert result.hits, "a graph outage must not empty the result set"
    assert result.related_entities == []
    assert all("entity_linked" not in hit.boosts for hit in result.hits)
    # Everything that does not depend on the graph is unaffected.
    assert all(hit.provenance.is_complete for hit in result.hits)
    assert result.context is not None


def test_the_degradation_warning_is_stored_in_the_retrieval_log(
    pg_session: Session, corpus: Corpus
) -> None:
    result = _search(_gateway(pg_session, graph=DownGraph()))

    row = pg_session.execute(
        sa.text("SELECT warnings FROM retrieval_logs WHERE id = :id"),
        {"id": result.retrieval_log_id},
    ).first()
    assert row is not None
    assert GRAPH_DEGRADED_WARNING in list(row[0])


def test_a_query_with_expansion_off_is_not_reported_as_degraded(
    pg_session: Session, corpus: Corpus, graph_up: UpGraph
) -> None:
    result = _search(_gateway(pg_session, graph=graph_up), expand=False)

    assert result.warnings == []
    assert result.related_entities == []
    assert graph_up.calls == []


def test_no_embedder_degrades_to_keyword_only_and_still_cites(
    pg_session: Session, corpus: Corpus, graph_up: UpGraph
) -> None:
    @contextmanager
    def scope() -> Iterator[Session]:
        yield pg_session

    settings = Settings()
    settings.gateway.write_enabled = False
    gateway = Gateway(scope, graph=graph_up, embedder=None, config=CONFIG, settings=settings)

    result = gateway.search(
        SearchQuery(query="retrieval pipeline pgvector", project_ids=[BIZ]), now=NOW
    )

    assert any("semantic" in warning for warning in result.warnings)
    assert result.candidate_counts["semantic"] == 0
    assert result.hits
    assert all(hit.citation for hit in result.hits)


# ------------------------------------------------------------------------------ retrieval_logs


def test_one_retrieval_log_row_is_written_per_query_with_the_replay_data(
    pg_session: Session, corpus: Corpus, graph_up: UpGraph
) -> None:
    result = _search(_gateway(pg_session, graph=graph_up))

    row = pg_session.execute(
        sa.text(
            "SELECT query_text, query_hash, project_ids, \"limit\", expand, params, "
            "candidate_counts, result_ids, latency_ms, client, warnings "
            "FROM retrieval_logs WHERE id = :id"
        ),
        {"id": result.retrieval_log_id},
    ).mappings().first()

    assert row is not None
    assert row["query_text"] == "retrieval pipeline pgvector"
    assert row["query_hash"].startswith("sha256:")
    assert list(row["project_ids"]) == [BIZ]
    assert row["expand"] is True
    assert row["latency_ms"] >= 0
    assert [UUID(str(i)) for i in row["result_ids"]] == [hit.object_id for hit in result.hits]
    assert row["candidate_counts"]["semantic"] >= 0
    assert row["candidate_counts"]["graph_seeds"] >= 1
    # The effective weights are stored, so a ranking change is attributable without a re-run.
    assert row["params"]["rrf_k"] == CONFIG.rrf_k
    assert row["params"]["project_match"] == CONFIG.boost_project_match


def test_the_client_tag_reaches_the_log(pg_session: Session, corpus: Corpus, graph_up: UpGraph) -> None:
    result = _search(_gateway(pg_session, graph=graph_up), client="pytest")

    client = pg_session.execute(
        sa.text("SELECT client FROM retrieval_logs WHERE id = :id"),
        {"id": result.retrieval_log_id},
    ).scalar_one()
    assert client == "pytest"


# ------------------------------------------------------------------------- the other read routes


def test_projects_carry_their_track_and_measured_coverage(pg_session: Session, corpus: Corpus) -> None:
    gateway = _gateway(pg_session)

    projects = {project.id: project for project in gateway.list_projects([BIZ, RES])}

    assert projects[BIZ].track == "business"
    assert projects[RES].track == "research"
    coverage = projects[BIZ].coverage
    assert coverage is not None
    assert coverage.sources_indexable == 2, "two INDEX_CONTENT sources were seeded for this project"
    assert coverage.sources_embedded == 2
    assert coverage.note, "coverage always says something a caller can read"


def test_an_unknown_project_is_a_not_found_not_an_empty_view(pg_session: Session, corpus: Corpus) -> None:
    with pytest.raises(NotFoundError):
        _gateway(pg_session).get_project("t-p10-does-not-exist")


def test_an_entity_returns_its_current_facts_and_its_mention_provenance(
    pg_session: Session, corpus: Corpus, graph_up: UpGraph
) -> None:
    view = _gateway(pg_session, graph=graph_up).get_entity(corpus.entities["pgvector"], as_of=NOW)

    assert view.canonical_name == "pgvector"
    assert view.track == "business"
    assert view.mention_count == 1
    assert [fact.statement for fact in view.facts] == ["pgvector depends on PostgreSQL"]
    assert view.facts[0].citation.startswith("[vault://p10/near.md#")
    assert view.provenance and view.provenance[0].is_complete
    assert [item.name for item in view.related] == ["PostgreSQL"]


def test_an_entity_still_answers_with_neo4j_down(pg_session: Session, corpus: Corpus) -> None:
    view = _gateway(pg_session, graph=DownGraph()).get_entity(corpus.entities["pgvector"])

    assert view.facts, "the facts come from PostgreSQL and are unaffected"
    assert view.related == []
    assert GRAPH_DEGRADED_WARNING in view.warnings


def test_decisions_are_current_by_default_and_superseded_on_request(
    pg_session: Session, corpus: Corpus
) -> None:
    gateway = _gateway(pg_session)

    current = gateway.get_decisions(project_ids=[BIZ], as_of=NOW)
    everything = gateway.get_decisions(project_ids=[BIZ], as_of=NOW, include_superseded=True)

    assert [d.title for d in current] == ["Use RRF for hybrid retrieval"]
    assert current[0].supersedes_title == "Use weighted score blending"
    assert current[0].citation.startswith("[vault://p10/")
    assert {d.title for d in everything} == {
        "Use RRF for hybrid retrieval",
        "Use weighted score blending",
    }


def test_an_as_of_before_the_supersession_returns_the_older_decision(
    pg_session: Session, corpus: Corpus
) -> None:
    older = _gateway(pg_session).get_decisions(
        project_ids=[BIZ], as_of=NOW - timedelta(days=100)
    )

    assert [d.title for d in older] == ["Use weighted score blending"]


def test_the_artifact_route_returns_both_ends_of_the_supersession_chain(
    pg_session: Session, corpus: Corpus
) -> None:
    gateway = _gateway(pg_session)

    new = gateway.get_artifact(corpus.artifacts["current"])
    old = gateway.get_artifact(corpus.artifacts["old"])

    assert new.supersedes_id == corpus.artifacts["old"]
    assert old.superseded_by_id == corpus.artifacts["current"]
    assert corpus.entities["pgvector"] in new.entity_ids


def test_the_timeline_merges_facts_artifacts_and_source_events(
    pg_session: Session, corpus: Corpus
) -> None:
    events = _gateway(pg_session).get_timeline(project_ids=[BIZ], limit=50)

    kinds = {event.kind for event in events}
    assert "artifact" in kinds
    assert "fact_opened" in kinds
    assert [event.at for event in events] == sorted((e.at for e in events), reverse=True)
    assert all(event.track == "business" for event in events)


def test_sources_return_uris_and_never_content(pg_session: Session, corpus: Corpus) -> None:
    sources = _gateway(pg_session).get_sources(project_ids=[BIZ])

    assert {source.uri for source in sources} == {"vault://p10/near.md", "vault://p10/mid.md"}
    assert all(source.content_hash and source.content_hash.startswith("sha256:") for source in sources)
    assert all(source.container_path == f"/sources/vault/{source.relative_path}" for source in sources)
    payload = [source.model_dump() for source in sources]
    assert all("text" not in item and "body" not in item and "data" not in item for item in payload)


def test_explain_walks_the_chain_back_to_the_device(pg_session: Session, corpus: Corpus) -> None:
    chain = _gateway(pg_session).explain(corpus.facts["uses"])

    kinds = [step.kind for step in chain.steps]
    assert kinds[0] == "fact"
    for expected in ("episode", "version", "source", "root", "device"):
        assert expected in kinds, f"the chain must reach {expected}"
    assert chain.complete is True
    assert chain.explanation


def test_explain_on_an_unknown_id_is_a_not_found(pg_session: Session, corpus: Corpus) -> None:
    with pytest.raises(NotFoundError):
        _gateway(pg_session).explain(_id("nothing-with-this-id"))


def test_current_state_reports_status_facts_tasks_decisions_and_coverage(
    pg_session: Session, corpus: Corpus
) -> None:
    state = _gateway(pg_session).get_current_state(project_ids=[BIZ, RES], as_of=NOW)

    assert {project.id for project in state.projects} == {BIZ, RES}
    assert [fact.statement for fact in state.current_facts] == ["pgvector depends on PostgreSQL"]
    assert [task.title for task in state.open_tasks] == ["Wire the Gateway into memory-api"]
    assert [d.title for d in state.latest_decisions] == ["Use RRF for hybrid retrieval"]
    assert state.by_track == {"business": [BIZ], "research": [RES]}
    assert state.coverage and all(item.note for item in state.coverage)
    assert state.last_ingestion is not None


# ------------------------------------------------------------------- writes (ADR-0008 gate)


def test_writes_are_refused_by_default(pg_session: Session, corpus: Corpus) -> None:
    """A disabled write raises *and* leaves no trace.

    The "no trace" half is measured as a delta, not an absolute. It used to assert that the database
    held no ``manual``/``mcp`` episodes at all, which was only ever true because writes had never
    been enabled; the first real MCP write (2026-09-20) made this test fail for a reason that had
    nothing to do with what it is checking.
    """
    count_sql = sa.text("SELECT count(*) FROM episodes WHERE type IN ('manual','mcp')")
    before = pg_session.execute(count_sql).scalar_one()

    gateway = _gateway(pg_session, writes=False)

    with pytest.raises(WriteDisabledError):
        gateway.add_episode(text="a manual note")
    with pytest.raises(WriteDisabledError):
        gateway.record_decision(title="A decision", body="Because.")

    assert pg_session.execute(count_sql).scalar_one() == before, (
        "a refused write leaves nothing behind"
    )


def test_an_enabled_write_appends_an_episode_tagged_with_its_client(
    pg_session: Session, corpus: Corpus
) -> None:
    receipt = _gateway(pg_session, writes=True).add_episode(
        text="An AI client wrote this.", title="note", project_id=BIZ, client="claude-desktop"
    )

    assert receipt.accepted is True
    row = pg_session.execute(
        sa.text("SELECT type, origin, origin_client, status, project_id, body FROM episodes "
                "WHERE id = :id"),
        {"id": receipt.episode_id},
    ).mappings().one()
    assert row["type"] == "mcp"
    assert row["origin"] == "external"
    assert row["origin_client"] == "claude-desktop"
    assert row["status"] == "pending", "extraction is queued, never run on the write path"


def test_record_decision_appends_and_links_instead_of_updating(
    pg_session: Session, corpus: Corpus
) -> None:
    gateway = _gateway(pg_session, writes=True)
    before = pg_session.execute(
        sa.text("SELECT title, current_status FROM knowledge_artifacts WHERE id = :id"),
        {"id": corpus.artifacts["current"]},
    ).mappings().one()

    receipt = gateway.record_decision(
        title="Use RRF with a recency cap",
        body="Cap the recency boost at +0.10.",
        project_id=BIZ,
        supersedes_id=corpus.artifacts["current"],
        client="claude-desktop",
    )

    assert receipt.accepted is True
    new = pg_session.execute(
        sa.text("SELECT title, supersedes_id, current_status, episode_id, device_id "
                "FROM knowledge_artifacts WHERE id = :id"),
        {"id": receipt.object_id},
    ).mappings().one()
    old = pg_session.execute(
        sa.text("SELECT title, current_status, superseded_by_id, valid_to "
                "FROM knowledge_artifacts WHERE id = :id"),
        {"id": corpus.artifacts["current"]},
    ).mappings().one()

    assert new["supersedes_id"] == corpus.artifacts["current"]
    assert new["episode_id"] == receipt.episode_id
    assert old["title"] == before["title"], "the superseded row keeps its text - append-only"
    assert old["current_status"] == "superseded"
    assert old["superseded_by_id"] == receipt.object_id
    assert old["valid_to"] is not None


def test_a_write_to_an_unknown_project_is_rejected_before_anything_is_inserted(
    pg_session: Session, corpus: Corpus
) -> None:
    gateway = _gateway(pg_session, writes=True)

    with pytest.raises(NotFoundError):
        gateway.add_episode(text="orphan", project_id="t-p10-nope")


def test_oversized_text_is_rejected(pg_session: Session, corpus: Corpus) -> None:
    from aimemory.common.errors import SchemaValidationError

    gateway = _gateway(pg_session, writes=True)
    limit = Settings().mcp.max_text_chars

    with pytest.raises(SchemaValidationError):
        gateway.add_episode(text="x" * (limit + 1))
